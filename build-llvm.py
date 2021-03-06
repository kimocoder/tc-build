#!/usr/bin/env python3
# Description: Builds an LLVM toolchain suitable for kernel development

import argparse
import datetime
import pathlib
import os
import subprocess
import shutil
import textwrap
import time
import utils


class Directories():
    def __init__(self, build_folder, install_folder, root_folder):
        self.build_folder = build_folder
        self.install_folder = install_folder
        self.root_folder = root_folder


class EnvVars():
    def __init__(self, cc, cxx, ld):
        self.cc = cc
        self.cxx = cxx
        self.ld = ld


def clang_version(cc):
    """
    Returns Clang's version as an integer
    :param cc: The compiler to check the version of
    :return: an int denoting the version of the given compiler
    """
    return int(subprocess.check_output(["./clang-version.sh", cc]).decode())


def parse_parameters(root_folder):
    """
    Parses parameters passed to the script into options
    :param root_folder: The directory where the script is being invoked from
    :return: A 'Namespace' object with all the options parsed from supplied parameters
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-b",
                        "--branch",
                        help=textwrap.dedent("""\
                        By default, the script builds the master branch (tip of tree) of LLVM. If you would
                        like to build an older branch, use this parameter. This may be helpful in tracking
                        down an older bug to properly bisect. This value is just passed along to 'git checkout'
                        so it can be a branch name, tag name, or hash.

                        """),
                        type=str,
                        default="master")
    parser.add_argument("-d",
                        "--debug",
                        help=textwrap.dedent("""\
                        By default, the script builds LLVM in the release configuration with all of
                        the tests turned off and optimization at O2. This disables that optimization,
                        builds the tests, and changes the configuration to debug. This can help with
                        reporting problems to LLVM developers but will make compilation of both LLVM
                        and the kernel go slower.

                        """),
                        action="store_true")
    parser.add_argument("-i",
                        "--incremental",
                        help=textwrap.dedent("""\
                        By default, the script removes all build artifacts from previous compiles. This
                        prevents that, allowing for dirty builds and faster compiles.

                        """),
                        action="store_true")
    parser.add_argument("-I",
                        "--install-folder",
                        help=textwrap.dedent("""\
                        By default, the script will create a "build" folder in the same folder as this script
                        and install the LLVM toolchain there. If you'd like to have it installed somewhere
                        else, pass it to this parameter. This can either be an absolute or relative path.

                        """),
                        type=str,
                        default=os.path.join(root_folder.as_posix(), "build"))
    parser.add_argument("-n",
                        "--no-pull",
                        help=textwrap.dedent("""\
                        By default, the script always updates the LLVM repo before building. This prevents
                        that, which can be helpful during something like bisecting.

                        """),
                        action="store_true")
    parser.add_argument("-p",
                        "--projects",
                        help=textwrap.dedent("""\
                        Currently, the script only enables the clang, compiler-rt, and lld folders in LLVM. If
                        you would like to override this, you can use this parameter and supply a list that is
                        supported by LLVM_ENABLE_PROJECTS.

                        See step #5 here: https://llvm.org/docs/GettingStarted.html#getting-started-quickly-a-summary

                        Example: -p \"clang;lld;libcxx\"

                        """),
                        type=str,
                        default="clang;lld;compiler-rt")
    parser.add_argument("-t",
                        "--targets",
                        help=textwrap.dedent("""\
                        LLVM is multitargeted by default. Currently, this script only enables the arm32, aarch64,
                        powerpc, and x86 backends because that's what the Linux kernel is currently concerned with.
                        If you would like to override this, you can use this parameter and supply a list that is
                        supported by LLVM_TARGETS_TO_BUILD: https://llvm.org/docs/CMake.html#llvm-specific-variables

                        Example: -t "AArch64;X86"

                        """),
                        type=str,
                        default="AArch64;ARM;PowerPC;X86")
    return parser.parse_args()


def linker_test(cc, ld):
    """
    Test to see if the supplied ld value will work with cc -fuse=ld
    :param cc: A working C compiler to compile the test program
    :param ld: A linker to test -fuse=ld against
    :return: 0 if the linker supports -fuse=ld, 1 otherwise
    """
    echo = subprocess.Popen(['echo', 'int main() { return 0; }'],
                            stdout=subprocess.PIPE)
    return subprocess.run(
        [cc, '-fuse-ld=' + ld, '-o', '/dev/null', '-x', 'c', '-'],
        stdin=echo.stdout,
        stderr=subprocess.DEVNULL).returncode


def check_cc_ld_variables():
    """
    Sets the cc, cxx, and ld variables, which will be passed to cmake
    :return: A tuple of valid cc, cxx, ld values that can be used to compile LLVM
    """
    utils.print_header("Checking CC and LD")
    cc, linker, ld = None, None, None
    # If the user specified a C compiler, get its full path
    if 'CC' in os.environ:
        cc = shutil.which(os.environ['CC'])
    # Otherwise, try to find one
    else:
        possible_compilers = ['clang-9', 'clang-8', 'clang-7', 'clang', 'gcc']
        for compiler in possible_compilers:
            cc = shutil.which(compiler)
            if cc is not None:
                break
        if cc is None:
            raise RuntimeError(
                "Neither gcc nor clang could be found on your system!")

    # Evaluate if CC is a symlink. Certain packages of clang (like from
    # apt.llvm.org) symlink the clang++ binary to clang++-<version> in
    # /usr/bin, which then points to something like /usr/lib/llvm-<version/bin.
    # This won't be found by the dumb logic below and trying to parse and figure
    # out a heuristic for that is a lot more effort than just going into the
    # folder that clang is actually installed in and getting clang++ from there.
    cc = os.path.realpath(cc)
    cc_folder = os.path.dirname(cc)

    # If the user specified a C++ compiler, get its full path
    if 'CXX' in os.environ:
        cxx = shutil.which(os.environ['CXX'])
    # Otherwise, use the one where CC is
    else:
        if "clang" in cc:
            cxx = "clang++"
        else:
            cxx = "g++"
        cxx = shutil.which(cxx, path=cc_folder + ":" + os.environ['PATH'])
    cxx = cxx.strip()

    # If the user specified a linker
    if 'LD' in os.environ:
        # evaluate its full path with clang to avoid weird issues and check to
        # see if it will work with '-fuse-ld', which is what cmake will do. Doing
        # it now prevents a hard error later.
        ld = os.environ['LD']
        if "clang" in cc and clang_version(cc) >= 30900:
            ld = shutil.which(ld)
        if linker_test(cc, ld):
            print("LD won't work with " + cc +
                  ", saving you from yourself by ignoring LD value")
            ld = None
    # If the user didn't specify a linker
    else:
        # and we're using clang, try to find the fastest one
        if "clang" in cc:
            possible_linkers = [
                'lld-9', 'lld-8', 'lld-7', 'lld', 'gold', 'bfd'
            ]
            for linker in possible_linkers:
                # We want to find lld wherever the clang we are using is located
                ld = shutil.which("ld." + linker,
                                  path=cc_folder + ":" + os.environ['PATH'])
                if ld is not None:
                    break
            # If clang is older than 3.9, it won't accept absolute paths so we
            # need to just pass it the name (and modify PATH so that it is found properly)
            # https://github.com/llvm/llvm-project/commit/e43b7413597d8102a4412f9de41102e55f4f2ec9
            if clang_version(cc) < 30900:
                os.environ['PATH'] = cc_folder + ":" + os.environ['PATH']
                ld = linker
        # and we're using gcc, try to use gold
        else:
            ld = "gold"
            if linker_test(cc, ld):
                ld = None

    # Print what binaries we are using to compile/link with so the user can
    # decide if that is proper or not
    print("CC: " + cc)
    print("CXX: " + cxx)
    if ld is not None:
        ld = ld.strip()
        ld_to_print = shutil.which("ld." + ld)
        if ld_to_print is None:
            ld_to_print = shutil.which(ld)
        print("LD: " + ld_to_print)

    return cc, cxx, ld


def check_dependencies():
    """
    Makes sure that the base dependencies of cmake, curl, git, and ninja are installed
    """
    utils.print_header("Checking dependencies")
    required_commands = ["cmake", "curl", "git", "ninja"]
    for command in required_commands:
        output = shutil.which(command)
        if output is None:
            raise RuntimeError(command +
                               " could not be found, please install it!")
        print(output)


def fetch_llvm_binutils(root_folder, update, ref):
    """
    Download llvm and binutils or update them if they exist
    :param root_folder: Working directory
    :param update: Boolean indicating whether sources need to be updated or not
    :param ref: The ref to checkout the monorepo to
    """
    p = root_folder.joinpath("llvm-project")
    if p.is_dir():
        if update:
            utils.print_header("Updating LLVM")
            subprocess.run(
                ["git", "-C", p.as_posix(), "checkout", ref], check=True)
            subprocess.run(
                ["git", "-C", p.as_posix(), "pull", "--rebase"], check=True)
    else:
        utils.print_header("Downloading LLVM")
        subprocess.run([
            "git", "clone", "-b", ref, "git://github.com/llvm/llvm-project",
            p.as_posix()
        ],
                       check=True)

    # One might wonder why we are downloading binutils in an LLVM build script :)
    # We need it for the LLVMgold plugin, which can be used for LTO with ld.gold,
    # which at the time of writing this, is how the Google Pixel 3 kernel is built
    # and linked.
    utils.download_binutils(root_folder)


def cleanup(build_folder, incremental):
    """
    Clean up and create the build folder
    :param build_folder: The build directory
    :param incremental: Whether the build is incremental or not.
    :return:
    """
    if not incremental and build_folder.is_dir():
        shutil.rmtree(build_folder.as_posix())
    build_folder.mkdir(parents=True, exist_ok=True)


def invoke_cmake(dirs, env_vars, debug, projects, targets):
    """
    Invoke cmake to generate the build files
    :param dirs: An instance of the Directories class with the paths to use
    :param debug: Boolean indicating if a debug toolchain is to be built
    :param env_vars: An instance of the EnvVars class with the compilers/linker to use
    :param projects: Projects to compile
    :param targets: Targets to compile
    :return:
    """
    utils.print_header("Configuring LLVM")

    build_folder = dirs.build_folder
    install_folder = dirs.install_folder
    root_folder = dirs.root_folder

    # Base cmake defintions, which don't depend on any user supplied options
    # yapf: disable
    defines = {
        # Objective-C Automatic Reference Counting (we don't use Objective-C)
        # https://clang.llvm.org/docs/AutomaticReferenceCounting.html
        'CLANG_ENABLE_ARCMT': 'OFF',
        # We don't (currently) use the static analyzer and it saves cycles
        # according to Chromium OS:
        # https://crrev.com/44702077cc9b5185fc21e99485ee4f0507722f82
        'CLANG_ENABLE_STATIC_ANALYZER': 'OFF',
        # We don't use the plugin system and it will remove unused symbols:
        # https://crbug.com/917404
        'CLANG_PLUGIN_SUPPORT': 'OFF',
        # The C compiler to use
        'CMAKE_C_COMPILER': env_vars.cc,
        # The C++ compiler to use
        'CMAKE_CXX_COMPILER': env_vars.cxx,
        # Where the toolchain should be installed
        'CMAKE_INSTALL_PREFIX': install_folder.as_posix(),
        # For LLVMgold.so, which is used for LTO with ld.gold
        'LLVM_BINUTILS_INCDIR': root_folder.joinpath(utils.current_binutils(), "include").as_posix(),
        # The projects to build
        'LLVM_ENABLE_PROJECTS': projects,
        # Don't build bindings; they are for other languages that the kernel does not use
        'LLVM_ENABLE_BINDINGS': 'OFF',
        # Don't build Ocaml documentation
        'LLVM_ENABLE_OCAMLDOC': 'OFF',
        # Removes system dependency on terminfo and almost every major clang provider turns this off
        'LLVM_ENABLE_TERMINFO': 'OFF',
        # Don't build clang-tools-extras to cut down on build targets (about 400 files or so)
        'LLVM_EXTERNAL_CLANG_TOOLS_EXTRA_SOURCE_DIR': '',
        # Don't include documentation build targets because it is available on the web
        'LLVM_INCLUDE_DOCS': 'OFF',
        # Don't include example build targets to save on cmake cycles
        'LLVM_INCLUDE_EXAMPLES': 'OFF',
        # The architectures to build backends for
        'LLVM_TARGETS_TO_BUILD': targets
    }
    # yapf: enable

    # If a debug build was requested
    if debug:
        defines['CMAKE_BUILD_TYPE'] = 'Debug'
        defines['CMAKE_C_FLAGS'] = '-march=native -mtune=native'
        defines['CMAKE_CXX_FLAGS'] = '-march=native -mtune=native'
        defines['LLVM_BUILD_TESTS'] = 'ON'
    # If a release build was requested
    else:
        defines['CMAKE_BUILD_TYPE'] = 'Release'
        defines['CMAKE_C_FLAGS'] = '-O2 -march=native -mtune=native'
        defines['CMAKE_CXX_FLAGS'] = '-O2 -march=native -mtune=native'
        defines['LLVM_INCLUDE_TESTS'] = 'OFF'
        defines['LLVM_ENABLE_WARNINGS'] = 'OFF'

    # Don't build libfuzzer when compiler-rt is enabled, it invokes cmake again and we don't use it
    if "compiler-rt" in projects:
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

    # Use ccache if it is available for faster incremental builds
    if shutil.which("ccache") is not None:
        defines['LLVM_CCACHE_BUILD'] = 'ON'

    # If we found a linker, we should use it
    if env_vars.ld is not None:
        defines['LLVM_USE_LINKER'] = env_vars.ld

    # Add the defines, point them to our build folder, and invoke cmake
    cmake = ['cmake', '-G', 'Ninja', '-Wno-dev']
    for key in defines:
        newdef = '-D' + key + '=' + defines[key]
        cmake += [newdef]
    cmake += [root_folder.joinpath("llvm-project", "llvm").as_posix()]

    subprocess.run(cmake, check=True, cwd=build_folder.as_posix())


def invoke_ninja(dirs):
    """
    Invoke ninja to run the actual build
    :param dirs: An instance of the Directories class with the paths to use
    :return:
    """
    utils.print_header("Building LLVM")

    build_folder = dirs.build_folder
    install_folder = dirs.install_folder

    time_started = time.time()

    subprocess.run('ninja', check=True, cwd=build_folder.as_posix())

    print()
    print("LLVM build duration: " +
          str(datetime.timedelta(seconds=int(time.time() - time_started))))

    subprocess.run(['ninja', 'install'],
                   check=True,
                   cwd=build_folder.as_posix(),
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

    utils.create_gitignore(install_folder)


def main():
    root_folder = pathlib.Path(__file__).resolve().parent
    build_folder = root_folder.joinpath("build", "llvm")

    args = parse_parameters(root_folder)

    install_folder = pathlib.Path(args.install_folder)
    if not install_folder.is_absolute():
        install_folder = root.joinpath(install_folder)

    env_vars = EnvVars(*check_cc_ld_variables())
    check_dependencies()
    fetch_llvm_binutils(root_folder, not args.no_pull, args.branch)
    cleanup(build_folder, args.incremental)
    dirs = Directories(build_folder, install_folder, root_folder)
    invoke_cmake(dirs, env_vars, args.debug, args.projects, args.targets)
    invoke_ninja(dirs)

    print("\nLLVM toolchain installed to: " + install_folder.as_posix())
    print("\nTo use, either run:\n")
    print("    $ export PATH=" + install_folder.as_posix() + ":${PATH}\n")
    print("or add:\n")
    print("    PATH=" + install_folder.as_posix() + ":${PATH}\n")
    print("to the command you want to use this toolchain.\n")


if __name__ == '__main__':
    main()
