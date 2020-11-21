""" Class to read and store all the data from the bucky input graph."""
import logging
from functools import partial

import networkx as nx

from .adjmat import buckyAij
from .numerical_libs import reimport_numerical_libs, xp
from .util.cached_prop import cached_property


class buckyGraphData:
    def __init__(self, G, sparse=True):

        reimport_numerical_libs()

        G = nx.convert_node_labels_to_integers(G)
        self.cum_case_hist, self.inc_case_hist = _read_node_attr(G, "case_hist", diff=True, a_min=0.0)
        self.cum_death_hist, self.inc_death_hist = _read_node_attr(G, "death_hist", diff=True, a_min=0.0)
        self.Nij = _read_node_attr(G, "N_age_init", a_min=1e-5)
        self.Nj = xp.sum(self.Nij, axis=0)

        # TODO add adm0 to support multiple countries
        self.adm2_id = _read_node_attr(G, G.graph["adm2_key"], dtype=int)[0]
        self.adm1_id = _read_node_attr(G, G.graph["adm1_key"], dtype=int)[0]

        # in case we want to alloc something indexed by adm1/2
        self.max_adm2 = xp.to_cpu(xp.max(self.adm2_id))
        self.max_adm1 = xp.to_cpu(xp.max(self.adm1_id))

        self.Aij = buckyAij(G, sparse)

        # TODO move this params to config?
        self._rolling_mean_type = "arithmetic"
        self._rolling_mean_window_size = 7
        self.rolling_mean_func = partial(
            rolling_mean, window_size=self._rolling_mean_window_size, axis=0, mean_type=self._rolling_mean_type
        )

    # TODO maybe provide a decorator or take a lambda or something to generalize it?
    # also this would be good if it supported rolling up to adm0 for multiple countries
    # memo so we don'y have to handle caching this on the input data?
    def sum_adm1(self, adm2_arr):
        # TODO add in axis param, we call this a bunch on array.T
        # assumes 1st dim is adm2 indexes
        shp = (self.max_adm1 + 1,) + adm2_arr.shape[1:]
        out = xp.zeros(shp, dtype=adm2_arr.dtype)
        xp.scatter_add(out, self.adm1_id, adm2_arr)
        return out

    # TODO other adm1 reductions (like harmonic mean)

    @cached_property
    def rolling_inc_cases(self):
        return self.rolling_mean_func(self.inc_case_hist)

    @cached_property
    def rolling_inc_deaths(self):
        return self.rolling_mean_func(self.inc_death_hist)

    @cached_property
    def rolling_cum_cases(self):
        return self.rolling_mean_func(self.cum_case_hist)

    @cached_property
    def rolling_cum_deaths(self):
        return self.rolling_mean_func(self.cum_death_hist)


def _read_node_attr(G, name, diff=False, dtype=float, a_min=None, a_max=None):
    clipping = (a_min is not None) or (a_max is not None)
    node_list = list(nx.get_node_attributes(G, name).values())
    arr = xp.vstack(node_list).astype(dtype).T
    if clipping:
        arr = xp.clip(arr, a_min=a_min, a_max=a_max)

    if diff:
        arr_diff = xp.diff(arr, axis=0).astype(dtype)
        if clipping:
            arr_diff = xp.clip(arr_diff, a_min=a_min, a_max=a_max)
        return arr, arr_diff

    return arr


# TODO move all these rolling mean stuff to util
def rolling_mean(arr, window_size=7, axis=0, weights=None, mean_type="arithmetic"):

    # we could probably just pass args/kwargs...
    if mean_type == "arithmetic":
        return rolling_arithmetic_mean(arr, window_size, axis, weights)
    elif mean_type == "geometric":
        return rolling_geometric_mean(arr, window_size, axis, weights)
    elif mean_type == "harmonic":
        return rolling_harmonic_mean(arr, window_size, axis, weights)
    else:
        raise RuntimeError  # TODO what type of err should go here?


# TODO move to util?
def rolling_arithmetic_mean(arr, window_size=7, axis=0, weights=None):
    arr = xp.swapaxes(arr, axis, -1)
    shp = arr.shape[:-1] + (arr.shape[-1] - window_size + 1,)
    rolling_arr = xp.empty(shp)
    if weights is None:
        window = xp.ones(window_size) / window_size
    else:
        window = weights / xp.sum(weights)
    arr = arr.reshape(-1, arr.shape[-1])
    for i in range(arr.shape[0]):
        rolling_arr[i] = xp.convolve(arr[i], window, mode="valid")
        rolling_arr = rolling_arr.reshape(shp)
    rolling_arr = xp.swapaxes(rolling_arr, axis, -1)
    return rolling_arr


def rolling_geometric_mean(arr, window_size, axis=0, weights=None):
    # add support for weights (need to use a log identity)
    if weights is not None:
        raise NotImplementedError
    arr = xp.swapaxes(arr, axis, -1)
    shp = arr.shape[:-1] + (arr.shape[-1] - window_size + 1,)
    rolling_arr = xp.empty(shp)
    window = xp.ones(window_size) / window_size
    arr = arr.reshape(-1, arr.shape[-1])
    log_abs_arr = xp.log(xp.abs(arr))
    neg_mask = arr < 0.0
    log_abs_arr[xp.abs(arr) < 1.0] = -1000.0
    for i in range(arr.shape[0]):
        tmp = xp.convolve(log_abs_arr[i], window, mode="valid")
        n_neg = xp.convolve(1.0 * neg_mask[i], xp.ones(window_size), mode="valid")
        # n_neg = xp.sum(arr[i] < 0.)
        rolling_arr[i] = ((-1.0) ** n_neg) ** (1.0 / window_size) * xp.exp(tmp)
    rolling_arr = rolling_arr.reshape(shp)
    rolling_arr = xp.swapaxes(rolling_arr, axis, -1)
    return rolling_arr


def rolling_harmonic_mean(arr, window_size, axis=0, weights=None):
    raise NotImplementedError
