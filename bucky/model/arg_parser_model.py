"""arg parser for bucky.model.

This module handles all the CLI argument parsing for bucky.model and autodetects CuPy.

"""
import argparse
import glob
import importlib
import logging
import os

from ..util.read_config import bucky_cfg

# TODO this logic should be in numerical_libs so we can apply it everywhere
if bool(os.getenv("BUCKY_CPU")):
    logging.info("BUCKY_CPU found, forcing cpu usage")
    force_cpu = True
else:
    force_cpu = False

cupy_found = (importlib.util.find_spec("cupy") is not None) and (not force_cpu)

most_recent_graph = max(
    glob.glob(bucky_cfg["data_dir"] + "/input_graphs/*.p"),
    key=os.path.getctime,
    default="Most recently created graph in <data_dir>/input_graphs",
)

parser = argparse.ArgumentParser(description="Bucky Model")

parser.add_argument(
    "--graph",
    "-g",
    dest="graph_file",
    default=most_recent_graph,
    type=str,
    help="Pickle file containing the graph to run",
)
parser.add_argument(
    "par_file",
    default=bucky_cfg["base_dir"] + "/par/",
    nargs="?",
    type=str,
    help="File/directory containing parameters",
)
parser.add_argument("--n_mc", "-n", default=100, type=int, help="Number of runs to do for Monte Carlo")
parser.add_argument("--days", "-d", default=40, type=int, help="Length of the runs in days")
parser.add_argument(
    "--seed",
    "-s",
    default=42,
    type=int,
    help="Initial seed to generate PRNG seeds from (doesn't need to be high entropy)",
)
parser.add_argument(
    "-v",
    "--verbose",
    action="count",
    dest="verbosity",
    default=0,
    help="verbose output (repeat for increased verbosity; defaults to WARN, -v is INFO, -vv is DEBUG)",
)
parser.add_argument(
    "-q",
    "--quiet",
    action="store_const",
    const=-1,
    default=0,
    dest="verbosity",
    help="quiet output (only show ERROR and higher)",
)

# TODO this doesnt do anything other than let you throw an error if there's no cupy...
# main checks args.gpu though so we need to have the value of cupy_found live
# in another namespace (numerical_libs?)
parser.add_argument(
    "-gpu",
    "--gpu",
    action="store_true",
    default=cupy_found,
    help="Use cupy instead of numpy",
)

parser.add_argument(
    "-opt",
    "--optimize",
    action="store_true",
    help="Run optimization over historical data",
)

parser.add_argument(
    "-optk",
    "--optimize_kernels",
    action="store_true",
    help="Enable cupy kernel optimizations. Do this for large runs using the gpu (n > 100).",
)

# TODO this should be able to take in the rejection factor thats hardcoded
parser.add_argument(
    "-r",
    "--reject_runs",
    action="store_true",
    help="Reject Monte Carlo runs with incidence rates that don't align with historical data",
)

parser.add_argument(
    "-o",
    "--output_dir",
    default=bucky_cfg["raw_output_dir"],
    type=str,
    help="Dir to put the output files",
)

parser.add_argument("--npi_file", default=None, nargs="?", type=str, help="File containing NPIs")
parser.add_argument(
    "--disable-npi",
    action="store_true",
    help="Disable all active NPI from the npi_file at the start of the run",
)
