"""Provide a class to hold the internal state vector to the compartment model (and track compartment indices)."""

import contextlib
import copy
import logging

from ..numerical_libs import sync_numerical_libs, xp
from .exceptions import StateValidationException


@sync_numerical_libs
def slice_to_cpu(s):
    """Ensure the values of the slice aren't cupy arrays to prevent an unsupported implict conversion in ``xp.r_``."""
    return xp.arange(xp.to_cpu(s.start), xp.to_cpu(s.stop), xp.to_cpu(s.step), dtype=xp.int32)
    # return slice(xp.to_cpu(s.start), xp.to_cpu(s.stop), xp.to_cpu(s.step))


class buckyState:  # pylint: disable=too-many-instance-attributes
    """Class to manage the state of the bucky compartments (and their indices)."""

    @sync_numerical_libs
    def __init__(self, consts, Nij, state=None):
        """Initialize the compartment indices and the state vector using the calling modules numerical libs."""

        self.E_gamma_k = consts["E_gamma_k"]
        self.I_gamma_k = consts["I_gamma_k"]
        self.Rh_gamma_k = consts["Rh_gamma_k"]
        self.consts = consts

        # Build a dict of bin counts per evolved compartment
        bin_counts = {}
        for name in ("S", "R", "D", "incH", "incC"):
            bin_counts[name] = 1
        for name in ("I", "Ic", "Ia"):
            bin_counts[name] = self.I_gamma_k
        bin_counts["E"] = self.E_gamma_k
        bin_counts["Rh"] = self.Rh_gamma_k

        # calculate slices for each compartment
        indices = {}
        current_index = 0
        for name, nbins in bin_counts.items():
            indices[name] = slice(current_index, current_index + nbins)
            current_index = current_index + nbins

        # define some combined compartment indices
        indices["N"] = xp.concatenate([xp.r_[slice_to_cpu(v)] for k, v in indices.items() if "inc" not in k])
        indices["Itot"] = xp.concatenate([xp.r_[slice_to_cpu(v)] for k, v in indices.items() if k in ("I", "Ia", "Ic")])
        indices["H"] = xp.concatenate([xp.r_[slice_to_cpu(v)] for k, v in indices.items() if k in ("Ic", "Rh")])

        self.indices = indices

        self.n_compartments = xp.to_cpu(sum(list(bin_counts.values()))).item()

        self.n_age_grps, self.n_nodes = Nij.shape

        if state is None:
            self.state = xp.zeros(self.state_shape, dtype=xp.float32)
        else:
            self.state = state

    def zeros_like(self):
        """Return a mostly shallow copy of self but with a zeroed out self.state."""
        ret = copy.copy(self)
        ret.state = xp.zeros_like(self.state)
        return ret

    def __getattribute__(self, attr):
        """Allow for . access to the compartment indices, otherwise return the 'normal' attribute."""
        with contextlib.suppress(AttributeError):
            if attr in super().__getattribute__("indices"):
                out = self.state[self.indices[attr]]
                if out.shape[0] == 1:
                    out = xp.squeeze(out, axis=0)
                return out

        return super().__getattribute__(attr)

    def __setattr__(self, attr, x):
        """Allow setting of compartments using . notation, otherwise default to normal attribute behavior."""
        try:
            if attr in super().__getattribute__("indices"):
                # TODO check that its a slice otherwise this wont work so we should warn
                self.state[self.indices[attr]] = x
            else:
                super().__setattr__(attr, x)
        except AttributeError:
            super().__setattr__(attr, x)

    @property
    def state_shape(self):
        """Return the shape of the internal state ndarray."""
        return (self.n_compartments, self.n_age_grps, self.n_nodes)

    def init_S(self):
        """Init the S compartment such that N=1."""
        self.S = 0.0
        self.S = 1.0 - xp.sum(self.N, axis=0)

    def validate_state(self):
        """Validate that the state is valid (finite, nonnegative, N=1)."""

        # Assert state is finite valued
        if xp.any(~xp.isfinite(self.state)):
            logging.debug(xp.argwhere(xp.any(~xp.isfinite(self.state), axis=0)))
            logging.info("nonfinite values in the state vector, something is wrong with init")
            raise StateValidationException

        # Assert N=1 in each sub model
        if xp.any(~(xp.around(xp.sum(self.N, axis=0), 2) == 1.0)):
            logging.debug(xp.argwhere(xp.any(~(xp.around(xp.sum(self.N, axis=0), 2) == 1.0), axis=0)))
            logging.info("N!=1 in the state vector, something is wrong with init")
            raise StateValidationException

        # Assert state is non negative
        if xp.any(~(xp.around(self.state, 4) >= 0.0)):
            logging.debug(xp.argwhere(xp.any(~(xp.around(self.state, 4) >= 0.0), axis=0)))
            logging.info("negative values in the state vector, something is wrong with init")
            raise StateValidationException
