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
    >>>     # Imports 
    >>>     import xarray as xr
    >>>     from xrspatial import curvature

    >>>     # Open Example DataArray
    >>>     agg = xr.open_dataarray('./docs/source/_static/nc/example_terrain.nc')

    >>>     print(agg)
    ...     <xarray.DataArray 'example_terrain' (lon: 600, lat: 800)>
    ...     [480000 values with dtype=float64]
    ...     Coordinates:
    ...       * lat      (lat) float64 -1.998e+07 -1.992e+07 ... 1.992e+07 1.997e+07
    ...       * lon      (lon) float64 -1.997e+07 -1.99e+07 ... 1.99e+07 1.997e+07
    ...     Attributes:
    ...         res:            1
    ...         Description:    Elevation
    ...         Max Elevation:  1000
    ...         units:          meters

    >>>     # Create Curvature Aggregate Array
    >>>     curvature_agg = curvature(agg)

    >>>     print(curvature_agg)
    ...     <xarray.DataArray 'curvature' (lon: 600, lat: 800)>
    ...     array([[nan, nan, nan, ..., nan, nan, nan],
    ...            [nan, -0., -0., ..., -0., -0., nan],
    ...            [nan, -0., -0., ..., -0., -0., nan],
    ...            ...,
    ...            [nan, -0., -0., ..., -0., -0., nan],
    ...            [nan, -0., -0., ..., -0., -0., nan],
    ...            [nan, nan, nan, ..., nan, nan, nan]])
    ...     Coordinates:
    ...       * lat      (lat) float64 -1.998e+07 -1.992e+07 ... 1.992e+07 1.997e+07
    ...       * lon      (lon) float64 -1.997e+07 -1.99e+07 ... 1.99e+07 1.997e+07
    ...     Attributes:
    ...         res:            1
    ...         Description:    Elevation
    ...         Max Elevation:  1000
    ...         units:          meters

    >>>     # View In A Jupyter Notebook
    >>>     from datashader.transfer_functions import shade
    >>>     shade(curvature_agg)
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
