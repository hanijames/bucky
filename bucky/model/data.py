"""Class to read and store all the data from the bucky input graph."""
import datetime
import logging
import warnings
from copy import deepcopy
from dataclasses import dataclass, field, fields
from functools import partial

import numpy as np
import pandas as pd
from joblib import Memory
from loguru import logger
from numpy import RankWarning

from ..data import AdminLevelMapping, CSSEData, HHSData
from ..data.clean_historical_data import clean_historical_data
from ..numerical_libs import sync_numerical_libs, xp
from ..util.array_utils import rolling_window
from ..util.cached_prop import cached_property
from ..util.extrapolate import interp_extrap
from ..util.power_transforms import YeoJohnson
from ..util.read_config import bucky_cfg
from ..util.spline_smooth import fit, lin_reg
from .adjmat import buckyAij

memory = Memory(bucky_cfg["cache_dir"], verbose=0, mmap_mode="r")


@memory.cache
def cached_scatter_add(a, slices, value):
    """scatter_add() thats cached by joblib."""
    ret = a.copy()
    xp.scatter_add(ret, slices, value)
    return ret


def read_population_tensor(file, return_adm2_ids=False, min_pop_per_bin=1.0):
    logger.debug("Reading census data from {}", file)
    census_df = pd.read_csv(
        file,
        index_col="adm2",
        engine="pyarrow",
    ).sort_index()
    ret = xp.clip(xp.array(census_df.values).astype(float), a_min=min_pop_per_bin, a_max=None).T
    if return_adm2_ids:
        return ret, xp.array(census_df.index)
    else:
        return ret


class buckyData:
    """Contains and preprocesses all the data imported from an input graph file."""

    @sync_numerical_libs
    def __init__(self, data_dir=None, force_diag_Aij=False, hist_length=101, force_historical_end_dow=4):
        """Initialize the input data into cupy/numpy, reading it from a networkx graph."""

        self.n_hist = hist_length

        # population data
        census_file = data_dir / "binned_census_age_groups.csv"
        self.Nij, self.adm2_id = read_population_tensor(census_file, return_adm2_ids=True)

        # adm-level mappings and bookkeeping
        self.adm1_id = self.adm2_id // 1000
        self.adm0_name = "US"

        self.adm_mapping = AdminLevelMapping(adm0="US", adm2_ids=self.adm2_id)

        # TODO need to remove these but ALOT of other code is still using this old way w/ sum_adm1
        self.max_adm1 = self.adm_mapping.n_adm1 - 1  # TODO remove (other things need this atm)
        # self.adm1_id = self.adm_mapping.adm1_ids

        # make adj mat obj
        self.Aij = buckyAij(n_nodes=self.Nij.shape[1], force_diag=force_diag_Aij)

        # CSSE case/death data
        csse_file = data_dir / "csse_timeseries.csv"
        self.raw_csse_data = CSSEData.from_csv(
            csse_file,
            n_days=self.n_hist,
            force_enddate_dow=force_historical_end_dow,
        )

        # TODO make these propeties that read form either csse_data of the fitted_data
        self.start_date = self.raw_csse_data.end_date  # TODO rename to sim_start_date or something...

        # HHS hospitalizations
        hhs_file = data_dir / "hhs_timeseries.csv"
        self.raw_hhs_data = HHSData.from_csv(hhs_file, n_days=self.n_hist, force_enddate_dow=force_historical_end_dow)

        # Prem contact matrices
        logger.debug("Loading Prem et al. matrices from {}", data_dir / "prem_matrices.csv")
        prem_df = pd.read_csv(
            data_dir / "prem_matrices.csv",
            index_col=["location", "i", "j"],
            engine="pyarrow",
        )
        self.Cij = {loc: xp.array(g_df.values).reshape(16, 16) for loc, g_df in prem_df.groupby("location")}

        logger.debug("Fitting GAM to historical timeseries")
        self.csse_data, self.hhs_data = clean_historical_data(self.raw_csse_data, self.raw_hhs_data, self.adm_mapping)

        # TODO remove once we switch everything over to hhs_data
        self.adm1_curr_hosp_hist = xp.empty((self.n_hist, self.max_adm1 + 1))
        self.adm1_inc_hosp_hist = xp.empty((self.n_hist, self.max_adm1 + 1))
        self.adm1_curr_hosp_hist[:, self.raw_hhs_data.adm_ids] = self.raw_hhs_data.current_hospitalizations
        self.adm1_inc_hosp_hist[:, self.hhs_data.adm_ids] = self.hhs_data.incident_hospitalizations

        # TODO need to remove this but ALOT of other code is still using this old way w/ sum_adm1
        self.max_adm1 = self.adm_mapping.n_adm1 - 1  # TODO remove (other things need this atm)

    # TODO maybe provide a decorator or take a lambda or something to generalize it?
    # also this would be good if it supported rolling up to adm0 for multiple countries
    # memo so we don'y have to handle caching this on the input data?
    # TODO! this should be operating on last index, its super fragmented atm
    # also if we sort node indices by adm2 that will at least bring them all together...
    def sum_adm1(self, adm2_arr, mask=None, cache=False):
        """Return the adm1 sum of a variable defined at the adm2 level using the mapping on the graph."""
        # TODO add in axis param, we call this a bunch on array.T
        # assumes 1st dim is adm2 indexes
        # TODO should take an axis argument and handle reshape, then remove all the transposes floating around
        # TODO we should use xp.unique(return_inverse=True) to compress these rather than
        #  allocing all the adm1 ids that dont exist, see the new postprocess
        # shp = (self.max_adm1 + 1,) + adm2_arr.shape[1:]
        shp = (self.adm_mapping.n_adm1,) + adm2_arr.shape[1:]
        out = xp.zeros(shp, dtype=adm2_arr.dtype)
        if mask is None:
            # adm1_ids = self.adm1_id
            adm1_ids = self.adm_mapping.adm1_ids
        else:
            adm1_ids = self.adm_mapping.adm1_ids[mask]
            # adm1_ids = self.adm1_id[mask]
            # adm2_arr = adm2_arr[mask]
        if cache:
            out = cached_scatter_add(out, adm1_ids, adm2_arr)
        else:
            xp.scatter_add(out, adm1_ids, adm2_arr)
        return out

    # TODO add scatter_adm2 with weights. Noone should need to check self.adm1/2_id outside this class

    # TODO other adm1 reductions (like harmonic mean), also add weights (for things like Nj)

    # Define and cache some of the reductions on Nij we might want
    @cached_property
    def Nj(self):
        r"""Total population per adm2.

        Notes
        -----
        .. math:: N_j = \sum_i N_{ij}

        Returns
        -------
        ndarray
        """
        return xp.sum(self.Nij, axis=0)

    @cached_property
    def N(self):
        """Total population."""
        return xp.sum(self.Nij)

    @cached_property
    def adm0_Ni(self):
        """Age stratified adm0 population."""
        return xp.sum(self.Nij, axis=1)

    @cached_property
    def adm1_Nij(self):
        """Age stratified adm1 populations."""
        return self.sum_adm1(self.Nij.T).T

    @cached_property
    def adm1_Nj(self):
        """Total adm1 populations."""
        return self.sum_adm1(self.Nj)

    # TODO these 4 are temporary until we fix everywhere they are used instead of csse_data
    @cached_property
    def cum_death_hist(self):
        return self.csse_data.cumulative_deaths

    @cached_property
    def inc_death_hist(self):
        return self.csse_data.incident_deaths

    @cached_property
    def cum_case_hist(self):
        return self.csse_data.cumulative_cases

    @cached_property
    def inc_case_hist(self):
        return self.csse_data.incident_cases

    # adm1 rollups of historical data
    @cached_property
    def adm1_cum_case_hist(self):
        """Cumulative cases by adm1."""
        return self.sum_adm1(self.cum_case_hist.T).T

    @cached_property
    def adm1_inc_case_hist(self):
        """Incident cases by adm1."""
        return self.sum_adm1(self.inc_case_hist.T).T

    @cached_property
    def adm1_cum_death_hist(self):
        """Cumulative deaths by adm1."""
        return self.sum_adm1(self.cum_death_hist.T).T

    @cached_property
    def adm1_inc_death_hist(self):
        """Incident deaths by adm1."""
        return self.sum_adm1(self.inc_death_hist.T).T

    # adm0 rollups of historical data
    @cached_property
    def adm0_cum_case_hist(self):
        """Cumulative cases at adm0."""
        return xp.sum(self.cum_case_hist, axis=1)

    @cached_property
    def adm0_inc_case_hist(self):
        """Incident cases at adm0."""
        return xp.sum(self.inc_case_hist, axis=1)

    @cached_property
    def adm0_cum_death_hist(self):
        """Cumulative deaths at adm0."""
        return xp.sum(self.cum_death_hist, axis=1)

    @cached_property
    def adm0_inc_death_hist(self):
        """Incident deaths at adm0."""
        return xp.sum(self.inc_death_hist, axis=1)
