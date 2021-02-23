# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""Validate environment states.

Example usage:

.. code-block::

    $ cat << EOF |
    benchmark,reward,walltime,commandline
    cBench-v0/crc32,0,1.2,opt  input.bc -o output.bc
    EOF
    python -m compiler_gym.bin.validate --env=llvm-ic-v0 -

Use this script to validate environment states. Environment states are read from
stdin as a comma-separated list of benchmark names, walltimes, episode rewards,
and commandlines. Each state is validated by replaying the commandline and
validating that the reward matches the expected value. Further, some benchmarks
allow for validation of program semantics. When available, those additional
checks will be automatically run.

Input Format
------------

The correct format for generating input states can be generated using
:func:`env.state.to_csv() <compiler_gym.envs.CompilerEnvState.to_csv>`. The
input CSV must start with a header row. A valid header row can be generated
using
:func:`env.state.csv_header() <compiler_gym.envs.CompilerEnvState.csv_header>`.

Full example:

>>> env = gym.make("llvm-v0")
>>> env.reset()
>>> env.step(0)
>>> print(env.state.csv_header())
benchmark,reward,walltime,commandline
>>> print(env.state.to_csv())
benchmark://cBench-v0/rijndael,,20.53565216064453,opt -add-discriminators input.bc -o output.bc
%

Output Format
-------------

This script prints one line per input state. The order of input states is not
preserved. A successfully validated state has the format:

.. code-block::

    ✅  <benchmark_name>  <reproduced_reward>

Else if validation fails, the output is:

.. code-block::

    ❌  <benchmark_name>  <error_details>
"""
import csv
import json
import re
import sys
from typing import Iterator

import numpy as np
from absl import app, flags

import compiler_gym.util.flags.dataset  # noqa Flag definition.
import compiler_gym.util.flags.nproc  # noqa Flag definition.
from compiler_gym.envs.compiler_env import CompilerEnvState
from compiler_gym.util.flags.env_from_flags import env_from_flags
from compiler_gym.util.shell_format import emph
from compiler_gym.util.statistics import geometric_mean
from compiler_gym.validate import ValidationResult, validate_states

flags.DEFINE_boolean(
    "inorder",
    False,
    "Whether to print results in the order they are provided. "
    "The default is to print results as soon as they are available.",
)
flags.DEFINE_string(
    "reward_aggregation",
    "geomean",
    "The aggregation method to use for rewards. Allowed values are 'mean' for "
    "arithmetic mean and 'geomean' for geometric mean.",
)
flags.DEFINE_boolean(
    "debug_force_valid",
    False,
    "Debugging flags. Skips the validation and prints output as if all states "
    "were succesfully validated.",
)
flags.DEFINE_boolean(
    "summary_only",
    False,
    "Do not print individual validation results, print only the summary at the " "end.",
)
flags.DEFINE_string(
    "validation_logfile",
    "validation.log.json",
    "The path of a file to write a JSON validation log to.",
)
FLAGS = flags.FLAGS


def read_states(in_file) -> Iterator[CompilerEnvState]:
    """Read the CSV states from stdin."""
    data = in_file.readlines()
    for line in csv.DictReader(data):
        try:
            line["reward"] = float(line["reward"]) if line.get("reward") else None
            line["walltime"] = float(line["walltime"]) if line.get("walltime") else None
            yield CompilerEnvState(**line)
        except (TypeError, KeyError) as e:
            print(f"Failed to parse input: `{e}`", file=sys.stderr)
            sys.exit(1)


def state_name(state: CompilerEnvState) -> str:
    """Get the string name for a state."""
    return re.sub(r"^benchmark://", "", state.benchmark)


def to_string(result: ValidationResult, name_col_width: int) -> str:
    """Format a validation result for printing."""
    name = state_name(result.state)

    if not result.okay():
        msg = ", ".join(result.error_details.strip().split("\n"))
        return f"❌  {name}  {msg}"
    elif result.state.reward is None:
        return f"✅  {name}"
    else:
        return f"✅  {name:<{name_col_width}}  {result.state.reward:9.4f}"


def arithmetic_mean(values):
    """Zero-length-safe arithmetic mean."""
    if not values:
        return 0
    return sum(values) / len(values)


def stdev(values):
    """Zero-length-safe standard deviation."""
    return np.std(values or [0])


def main(argv):
    """Main entry point."""
    # Parse the input states from the user.
    states = []
    for path in argv[1:]:
        if path == "-":
            states += list(read_states(sys.stdin))
        else:
            with open(path) as f:
                states += list(read_states(f))

    if not states:
        print(
            "No inputs to validate. Pass a CSV file path as an argument, or "
            "use - to read from stdin.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Send the states off for validation
    if FLAGS.debug_force_valid:
        validation_results = (
            ValidationResult(
                state=state,
                reward_validated=True,
                actions_replay_failed=False,
                reward_validation_failed=False,
                benchmark_semantics_validated=False,
                benchmark_semantics_validation_failed=False,
                walltime=0,
            )
            for state in states
        )
    else:
        validation_results = validate_states(
            env_from_flags,
            states,
            datasets=FLAGS.dataset,
            nproc=FLAGS.nproc,
            inorder=FLAGS.inorder,
        )

    # Determine the name of the reward space.
    env = env_from_flags()
    try:
        if FLAGS.reward_aggregation == "geomean":

            def reward_aggregation(a):
                return geometric_mean(np.clip(a, 0, None))

            reward_aggregation_name = "Geometric mean"
        elif FLAGS.reward_aggregation == "mean":
            reward_aggregation = arithmetic_mean
            reward_aggregation_name = "Mean"
        else:
            raise app.UsageError(
                f"Unknown aggregation type: '{FLAGS.reward_aggregation}'"
            )

        if env.reward_space:
            reward_name = f"{reward_aggregation_name} {env.reward_space.id}"
        else:
            reward_name = ""
    finally:
        env.close()

    # Determine the maximum column width required for printing tabular output.
    max_state_name_length = max(
        len(s)
        for s in [state_name(s) for s in states]
        + [
            "Mean inference walltime",
            reward_name,
        ]
    )
    name_col_width = min(max_state_name_length + 2, 78)

    error_count = 0
    rewards = []
    walltimes = []

    if FLAGS.summary_only:

        def intermediate_print(*args, **kwargs):
            pass

    else:
        intermediate_print = print

    def plural(quantity, singular, plural):
        return singular if quantity == 1 else plural

    def progress_message(i):
        intermediate_print(
            f"{i} remaining {plural(i, 'state', 'states')} to validate ... ",
            end="",
            flush=True,
        )

    progress_message(0)
    json_log = []

    def dump_json_log():
        with open(FLAGS.validation_logfile, "w") as f:
            json.dump(json_log, f)

    for i, result in enumerate(validation_results, start=1):
        intermediate_print("\r\033[K", to_string(result, name_col_width), sep="")
        progress_message(len(states) - i)
        json_log.append(result.json())

        if not result.okay():
            error_count += 1
        elif result.reward_validated and not result.reward_validation_failed:
            rewards.append(result.state.reward)
            walltimes.append(result.state.walltime)

        if not i % 10:
            dump_json_log()

    dump_json_log()

    # Print a summary footer.
    intermediate_print("\r\033[K----", "-" * name_col_width, "-----------", sep="")
    print(f"Number of validated results: {emph(len(walltimes))} of {len(states)}")
    walltime_mean = f"{arithmetic_mean(walltimes):.3f}s"
    walltime_std = f"{stdev(walltimes):.3f}s"
    print(
        f"Mean walltime per benchmark: {emph(walltime_mean)} "
        f"(std: {emph(walltime_std)})"
    )
    reward = f"{reward_aggregation(rewards):.3f}"
    reward_std = f"{stdev(rewards):.3f}"
    print(f"{reward_name}: {emph(reward)} " f"(std: {emph(reward_std)})")

    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    app.run(main)
