# std lib
from functools import partial
from typing import Union

# 3rd-party
try:
    import cupy
except ImportError:
    class cupy(object):
        ndarray = False

import dask.array as da

from numba import cuda

import numpy as np
import xarray as xr

# local modules
from xrspatial.utils import cuda_args
from xrspatial.utils import get_dataarray_resolution
from xrspatial.utils import has_cuda
from xrspatial.utils import ngjit
from xrspatial.utils import is_cupy_backed

from typing import Optional


@ngjit
def _cpu(data, cellsize):
    out = np.empty(data.shape, np.float64)
    out[:, :] = np.nan
    rows, cols = data.shape
    for y in range(1, rows - 1):
        for x in range(1, cols - 1):
            d = (data[y + 1, x] + data[y - 1, x]) / 2 - data[y, x]
            e = (data[y, x + 1] + data[y, x - 1]) / 2 - data[y, x]
            out[y, x] = -2 * (d + e) * 100 / (cellsize * cellsize)
    return out


def _run_numpy(data: np.ndarray,
               cellsize: Union[int, float]) -> np.ndarray:
    # TODO: handle border edge effect
    out = _cpu(data, cellsize)
    return out


def _run_dask_numpy(data: da.Array,
                    cellsize: Union[int, float]) -> da.Array:
    _func = partial(_cpu,
                    cellsize=cellsize)

    out = data.map_overlap(_func,
                           depth=(1, 1),
                           boundary=np.nan,
                           meta=np.array(()))
    return out


@cuda.jit(device=True)
def _gpu(arr, cellsize):
    d = (arr[1, 0] + arr[1, 2]) / 2 - arr[1, 1]
    e = (arr[0, 1] + arr[2, 1]) / 2 - arr[1, 1]
    curv = -2 * (d + e) * 100 / (cellsize[0] * cellsize[0])
    return curv


@cuda.jit
def _run_gpu(arr, cellsize, out):
    i, j = cuda.grid(2)
    di = 1
    dj = 1
    if (i - di >= 0 and i + di <= out.shape[0] - 1 and
            j - dj >= 0 and j + dj <= out.shape[1] - 1):
        out[i, j] = _gpu(arr[i - di:i + di + 1, j - dj:j + dj + 1], cellsize)


def _run_cupy(data: cupy.ndarray,
              cellsize: Union[int, float]) -> cupy.ndarray:

    cellsize_arr = cupy.array([float(cellsize)], dtype='f4')

    # TODO: add padding
    griddim, blockdim = cuda_args(data.shape)
    out = cupy.empty(data.shape, dtype='f4')
    out[:] = cupy.nan

    _run_gpu[griddim, blockdim](data, cellsize_arr, out)

    return out


def _run_dask_cupy(data: da.Array,
                   cellsize: Union[int, float]) -> da.Array:
    msg = 'Upstream bug in dask prevents cupy backed arrays'
    raise NotImplementedError(msg)


def curvature(agg: xr.DataArray,
              name: Optional[str] = 'curvature') -> xr.DataArray:
    """
    Calculates, for all cells in the array, the curvature (second
    derivative) of each cell based on the elevation of its neighbors
    in a 3x3 grid. A positive curvature indicates the surface is
    upwardly convex. A negative value indicates it is upwardly
    concave. A value of 0 indicates a flat surface.

    Units of the curvature output raster are one hundredth (1/100)
    of a z-unit.

    Parameters
    ----------
    agg : xarray.DataArray
        2D NumPy, CuPy, NumPy-backed Dask, or Cupy-backed Dask array
        of elevation values.
        Must contain "res" attribute.
    name : str, default = "curvature"
        Name of output DataArray.

    Returns
    -------
    curvature_agg : xarray.DataArray, of the same type as `agg`.
        2D aggregate array of curvature values.
        All other input attributes are preserved.

    Notes
    -----
    Algorithm References
        - https://pro.arcgis.com/en/pro-app/latest/tool-reference/spatial-analyst/how-curvature-works.htm

    Example
    -------
    >>>     import datashader as ds
    >>>     import numpy as np
    >>>     from xrspatial import generate_terrain, curvature
    >>>     from datashader.transfer_functions import shade, stack
    >>>     from datashader.colors import Elevation

    >>>     # Create Canvas
    >>>     W = 500 
    >>>     H = 300
    >>>     cvs = ds.Canvas(plot_width = W,
    >>>                     plot_height = H,
    >>>                     x_range = (-20e6, 20e6),
    >>>                     y_range = (-20e6, 20e6))
    >>>     # Generate Example Terrain
    >>>     terrain_agg = generate_terrain(canvas = cvs)
    >>>     terrain_agg = terrain_agg.assign_attrs({'Description': 'Elevation',
    >>>                                             'Max Elevation': '3000',
    >>>                                             'units': 'meters'})
    >>>     terrain_agg = terrain_agg.rename({'x': 'lon', 'y': 'lat'})
    >>>     terrain_agg = terrain_agg.rename('example_terrain')
    >>>     # Shade Terrain
    >>>     terrain_img = shade(agg = terrain_agg,
    >>>                         cmap = ['grey', 'white'],
    >>>                         how = 'linear')
    >>>     print(terrain_agg[200:203, 200:202])
    >>>     terrain_img
    ...     <xarray.DataArray 'example_terrain' (lat: 3, lon: 2)>
    ...     array([[1264.02249454, 1261.94748873],
    ...            [1285.37061171, 1282.48046696],
    ...            [1306.02305679, 1303.40657515]])
    ...     Coordinates:
    ...       * lon      (lon) float64 -3.96e+06 -3.88e+06
    ...       * lat      (lat) float64 6.733e+06 6.867e+06 7e+06
    ...     Attributes:
    ...         res:            1
    ...         Description:    Elevation
    ...         Max Elevation:  3000
    ...         units:          meters

            .. image :: ./docs/source/_static/img/docstring/terrain_example_grey.png

    >>>     # Create Curvature Aggregate Array
    >>>     curvature_agg = curvature(agg = terrain_agg)
    >>>     # Where cells are extremely upwardly convex
    >>>     where_clause = (curvature_agg > 3000)
    >>>     # Shade Image
    >>>     curvature_img = shade(agg = curvature_agg.where(where_clause),
    >>>                           alpha = 200,
    >>>                           cmap = ['green'])
    >>>     print(curvature_agg[200:203, 200:202])
    >>>     curvature_img
    ...     <xarray.DataArray 'curvature' (lat: 3, lon: 2)>
    ...     array([[-926.49565188, -622.23551544],
    ...            [ 324.53310176,  141.65868176],
    ...            [1013.96844315, 1803.27526745]])
    ...     Coordinates:
    ...       * lon      (lon) float64 -3.96e+06 -3.88e+06
    ...       * lat      (lat) float64 6.733e+06 6.867e+06 7e+06
    ...     Attributes:
    ...         res:            1
    ...         Description:    Elevation
    ...         Max Elevation:  3000
    ...         units:          meters

            .. image :: ./docs/source/_static/img/docstring/curvature_example.png

    >>>     # Combine Images
    >>>     composite_img = stack(terrain_img, curvature_img)
    >>>     composite_img

            .. image :: ./docs/source/_static/img/docstring/curvature_composite.png

    """

    cellsize_x, cellsize_y = get_dataarray_resolution(agg)
    cellsize = (cellsize_x + cellsize_y) / 2

    # numpy case
    if isinstance(agg.data, np.ndarray):
        out = _run_numpy(agg.data, cellsize)

    # cupy case
    elif has_cuda() and isinstance(agg.data, cupy.ndarray):
        out = _run_cupy(agg.data, cellsize)

    # dask + cupy case
    elif has_cuda() and isinstance(agg.data, da.Array) and is_cupy_backed(agg):
        out = _run_dask_cupy(agg.data, cellsize)

    # dask + numpy case
    elif isinstance(agg.data, da.Array):
        out = _run_dask_numpy(agg.data, cellsize)

    else:
        raise TypeError('Unsupported Array Type: {}'.format(type(agg.data)))

    return xr.DataArray(out,
                        name=name,
                        coords=agg.coords,
                        dims=agg.dims,
                        attrs=agg.attrs)
