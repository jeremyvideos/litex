# This file is Copyright (c) 2018 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 David Shah <dave@ds0.me>
# This file is Copyright (c) 2018 William D. Jones <thor0505@comcast.net>
# License: BSD

import os
import subprocess
import sys

from migen.fhdl.structure import _Fragment

from litex.build.generic_platform import *
from litex.build import tools
from litex.build.lattice import common

# TODO:
# - check/document attr_translate.

nextpnr_ecp5_architectures = {
    "lfe5u-25f": "25k",
    "lfe5u-45f": "45k",
    "lfe5u-85f": "85k",
    "lfe5um-25f": "um-25k",
    "lfe5um-45f": "um-45k",
    "lfe5um-85f": "um-85k",
    "lfe5um5g-25f": "um5g-25k",
    "lfe5um5g-45f": "um5g-45k",
    "lfe5um5g-85f": "um5g-85k",
}


def nextpnr_ecp5_package(package):
    if "285" in package:
        return "CSFBGA285"
    elif "381" in package:
        return "CABGA381"
    elif "554" in package:
        return "CABGA554"
    elif "756" in package:
        return "CABGA756"
    raise ValueError("Unknown package")


def _format_constraint(c):
    if isinstance(c, Pins):
        return ("LOCATE COMP ", " SITE " + "\"" + c.identifiers[0] + "\"")
    elif isinstance(c, IOStandard):
        return ("IOBUF PORT ", " IO_TYPE=" + c.name)
    elif isinstance(c, Misc):
        return ("IOBUF PORT ", " " + c.misc)


def _format_lpf(signame, pin, others, resname):
    fmt_c = [_format_constraint(c) for c in ([Pins(pin)] + others)]
    r = ""
    for pre, suf in fmt_c:
        r += pre + "\"" + signame + "\"" + suf + ";\n"
    return r


def _build_lpf(named_sc, named_pc):
    r = "BLOCK RESETPATHS;\n"
    r += "BLOCK ASYNCPATHS;\n"
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += _format_lpf(sig + "[" + str(i) + "]", p, others, resname)
        else:
            r += _format_lpf(sig, pins[0], others, resname)
    if named_pc:
        r += "\n" + "\n\n".join(named_pc)
    return r


def _build_script(source, build_template, build_name, architecture,
                  package, freq_constraint, timingstrict):
    if sys.platform in ("win32", "cygwin"):
        script_ext = ".bat"
        build_script_contents = "@echo off\nrem Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n\n"
        fail_stmt = " || exit /b"
    else:
        script_ext = ".sh"
        build_script_contents = "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\nset -e\n"
        fail_stmt = ""

    for s in build_template:
        s_fail = s + "{fail_stmt}\n"  # Required so Windows scripts fail early.
        build_script_contents += s_fail.format(build_name=build_name,
                                               architecture=architecture,
                                               package=package,
                                               freq_constraint=freq_constraint,
                                               timefailarg="--timing-allow-fail" if not timingstrict else "",
                                               fail_stmt=fail_stmt)

    build_script_file = "build_" + build_name + script_ext
    tools.write_to_file(build_script_file, build_script_contents,
                        force_unix=False)
    return build_script_file


def _run_script(script):
    if sys.platform in ("win32", "cygwin"):
        shell = ["cmd", "/c"]
    else:
        shell = ["bash"]

    if subprocess.call(shell + [script]) != 0:
        raise OSError("Subprocess failed")


def yosys_import_sources(platform):
    includes = ""
    reads = []
    for path in platform.verilog_include_paths:
        includes += " -I" + path
    for filename, language, library in platform.sources:
        reads.append("read_{}{} {}".format(
            language, includes, filename))
    return "\n".join(reads)


class LatticeTrellisToolchain:
    attr_translate = {
        # FIXME: document
        "keep": ("keep", "true"),
        "no_retiming": None,
        "async_reg": None,
        "mr_ff": None,
        "mr_false_path": None,
        "ars_ff1": None,
        "ars_ff2": None,
        "ars_false_path": None,
        "no_shreg_extract": None
    }

    special_overrides = common.lattice_ecpx_trellis_special_overrides

    def __init__(self):
        self.yosys_template = [
            "{read_files}",
            "attrmap -tocase keep -imap keep=\"true\" keep=1 -imap keep=\"false\" keep=0 -remove keep=0",
            "synth_ecp5 -abc9 {nwl} -json {build_name}.json -top {build_name}",
        ]

        self.build_template = [
            "yosys -q -l {build_name}.rpt {build_name}.ys",
            "nextpnr-ecp5 --json {build_name}.json --lpf {build_name}.lpf --textcfg {build_name}.config --{architecture} --package {package} --freq {freq_constraint} {timefailarg}",
            "ecppack {build_name}.config --svf {build_name}.svf --bit {build_name}.bit"
        ]

        self.freq_constraints = dict()

    def build(self, platform, fragment, build_dir="build", build_name="top",
              toolchain_path=None, run=True,
              nowidelut=False, timingstrict=False,
              **kwargs):
        if toolchain_path is None:
            toolchain_path = "/usr/share/trellis/"
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        # generate verilog
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)

        top_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(top_output.ns)
        top_file = build_name + ".v"
        top_output.write(top_file)
        platform.add_source(top_file)

        # generate constraints
        tools.write_to_file(build_name + ".lpf",
                            _build_lpf(named_sc, named_pc))

        # generate yosys script
        yosys_script_file = build_name + ".ys"
        yosys_script_contents = "\n".join(_.format(build_name=build_name,
                                                   nwl="-nowidelut" if nowidelut else "",
                                                   read_files=yosys_import_sources(platform))
                                          for _ in self.yosys_template)
        tools.write_to_file(yosys_script_file, yosys_script_contents)

        # transform platform.device to nextpnr's architecture
        (family, size, package) = platform.device.split("-")
        architecture = nextpnr_ecp5_architectures[(family + "-" + size).lower()]
        package = nextpnr_ecp5_package(package)
        freq_constraint = str(max(self.freq_constraints.values(),
                                  default=0.0))

        script = _build_script(False, self.build_template, build_name,
                               architecture, package, freq_constraint,
                               timingstrict)

        # run scripts
        if run:
            _run_script(script)

        os.chdir(cwd)

        return top_output.ns

    # Until nextpnr-ecp5 can handle multiple clock domains, use the same
    # approach as the icestorm and use the fastest clock for timing
    # constraints.
    def add_period_constraint(self, platform, clk, period):
        platform.add_platform_command("""FREQUENCY PORT "{clk}" {freq} MHz;""".format(freq=str(float(1/period)*1000), clk="{clk}"), clk=clk)

def trellis_args(parser):
    parser.add_argument("--yosys-nowidelut", action="store_true",
                        help="pass '-nowidelut' to yosys synth_ecp5")
    parser.add_argument("--nextpnr-timingstrict", action="store_true",
                        help="fail if timing not met, i.e., do NOT pass '--timing-allow-fail' to nextpnr")

def trellis_argdict(args):
    return {
        "nowidelut": args.yosys_nowidelut,
        "timingstrict": args.nextpnr_timingstrict,
    }
