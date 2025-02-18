# Hooks to customize how EasyBuild installs software in EESSI
# see https://docs.easybuild.io/en/latest/Hooks.html
import os
import re

import easybuild.tools.environment as env
from easybuild.easyblocks.generic.configuremake import obtain_config_guess
from easybuild.framework.easyconfig.constants import EASYCONFIG_CONSTANTS
from easybuild.easyblocks.python import EXTS_FILTER_PYTHON_PACKAGES
from easybuild.tools.build_log import EasyBuildError, print_msg
from easybuild.tools.config import build_option, update_build_option
from easybuild.tools.filetools import apply_regex_substitutions, copy_file, remove_file, symlink, which
from easybuild.tools.run import run_cmd
from easybuild.tools.systemtools import AARCH64, POWER, X86_64, get_cpu_architecture, get_cpu_features
from easybuild.tools.toolchain.compiler import OPTARCH_GENERIC

# prefer importing LooseVersion from easybuild.tools, but fall back to distuils in case EasyBuild <= 4.7.0 is used
try:
    from easybuild.tools import LooseVersion
except ImportError:
    from distutils.version import LooseVersion


CPU_TARGET_NEOVERSE_N1 = 'aarch64/neoverse_n1'
CPU_TARGET_NEOVERSE_V1 = 'aarch64/neoverse_v1'
CPU_TARGET_AARCH64_GENERIC = 'aarch64/generic'

EESSI_RPATH_OVERRIDE_ATTR = 'orig_rpath_override_dirs'

SYSTEM = EASYCONFIG_CONSTANTS['SYSTEM'][0]


def get_eessi_envvar(eessi_envvar):
    """Get an EESSI environment variable from the environment"""

    eessi_envvar_value = os.getenv(eessi_envvar)
    if eessi_envvar_value is None:
        raise EasyBuildError("$%s is not defined!", eessi_envvar)

    return eessi_envvar_value


def get_rpath_override_dirs(software_name):
    # determine path to installations in software layer via $EESSI_SOFTWARE_PATH
    eessi_software_path = get_eessi_envvar('EESSI_SOFTWARE_PATH')

    # construct the rpath override directory stub
    rpath_injection_stub = os.path.join(
        # Make sure we are looking inside the `host_injections` directory
        eessi_software_path.replace('versions', 'host_injections', 1),
        # Add the subdirectory for the specific software
        'rpath_overrides',
        software_name,
        # We can't know the version, but this allows the use of a symlink
        # to facilitate version upgrades without removing files
        'system',
    )

    # Allow for libraries in lib or lib64
    rpath_injection_dirs = [os.path.join(rpath_injection_stub, x) for x in ('lib', 'lib64')]

    return rpath_injection_dirs


def parse_hook(ec, *args, **kwargs):
    """Main parse hook: trigger custom functions based on software name."""

    # determine path to Prefix installation in compat layer via $EPREFIX
    eprefix = get_eessi_envvar('EPREFIX')

    if ec.name in PARSE_HOOKS:
        PARSE_HOOKS[ec.name](ec, eprefix)

    # inject the GPU property (if required)
    ec = inject_gpu_property(ec)


def post_ready_hook(self, *args, **kwargs):
    """
    Post-ready hook: limit parallellism for selected builds, because they require a lot of memory per used core.
    """
    # 'parallel' easyconfig parameter is set via EasyBlock.set_parallel in ready step based on available cores.
    # here we reduce parallellism to only use half of that for selected software,
    # to avoid failing builds/tests due to out-of-memory problems
    if self.name in ['TensorFlow', 'libxc']:
        parallel = self.cfg['parallel']
        if parallel > 1:
            self.cfg['parallel'] = parallel // 2
            msg = "limiting parallelism to %s (was %s) for %s to avoid out-of-memory failures during building/testing"
            print_msg(msg % (self.cfg['parallel'], parallel, self.name), log=self.log)


def pre_prepare_hook(self, *args, **kwargs):
    """Main pre-prepare hook: trigger custom functions."""

    # Check if we have an MPI family in the toolchain (returns None if there is not)
    mpi_family = self.toolchain.mpi_family()

    # Inject an RPATH override for MPI (if needed)
    if mpi_family:
        # Get list of override directories
        mpi_rpath_override_dirs = get_rpath_override_dirs(mpi_family)

        # update the relevant option (but keep the original value so we can reset it later)
        if hasattr(self, EESSI_RPATH_OVERRIDE_ATTR):
            raise EasyBuildError("'self' already has attribute %s! Can't use pre_prepare hook.",
                                 EESSI_RPATH_OVERRIDE_ATTR)

        setattr(self, EESSI_RPATH_OVERRIDE_ATTR, build_option('rpath_override_dirs'))
        if getattr(self, EESSI_RPATH_OVERRIDE_ATTR):
            # self.EESSI_RPATH_OVERRIDE_ATTR is (already) a colon separated string, let's make it a list
            orig_rpath_override_dirs = [getattr(self, EESSI_RPATH_OVERRIDE_ATTR)]
            rpath_override_dirs = ':'.join(orig_rpath_override_dirs + mpi_rpath_override_dirs)
        else:
            rpath_override_dirs = ':'.join(mpi_rpath_override_dirs)
        update_build_option('rpath_override_dirs', rpath_override_dirs)
        print_msg("Updated rpath_override_dirs (to allow overriding MPI family %s): %s",
                  mpi_family, rpath_override_dirs)

    if self.name in PRE_PREPARE_HOOKS:
        PRE_PREPARE_HOOKS[self.name](self, *args, **kwargs)


def post_prepare_hook_gcc_prefixed_ld_rpath_wrapper(self, *args, **kwargs):
    """
    Post-configure hook for GCCcore:
    - copy RPATH wrapper script for linker commands to also have a wrapper in place with system type prefix like 'x86_64-pc-linux-gnu'
    """
    if self.name == 'GCCcore':
        config_guess = obtain_config_guess()
        system_type, _ = run_cmd(config_guess, log_all=True)
        cmd_prefix = '%s-' % system_type.strip()
        for cmd in ('ld', 'ld.gold', 'ld.bfd'):
            wrapper = which(cmd)
            self.log.info("Path to %s wrapper: %s" % (cmd, wrapper))
            wrapper_dir = os.path.dirname(wrapper)
            prefix_wrapper = os.path.join(wrapper_dir, cmd_prefix + cmd)
            copy_file(wrapper, prefix_wrapper)
            self.log.info("Path to %s wrapper with '%s' prefix: %s" % (cmd, cmd_prefix, which(prefix_wrapper)))

            # we need to tweak the copied wrapper script, so that:
            regex_subs = [
                # - CMD in the script is set to the command name without prefix, because EasyBuild's rpath_args.py
                #   script that is used by the wrapper script only checks for 'ld', 'ld.gold', etc.
                #   when checking whether or not to use -Wl
                ('^CMD=.*', 'CMD=%s' % cmd),
                # - the path to the correct actual binary is logged and called
                ('/%s ' % cmd, '/%s ' % (cmd_prefix + cmd)),
            ]
            apply_regex_substitutions(prefix_wrapper, regex_subs)
    else:
        raise EasyBuildError("GCCcore-specific hook triggered for non-GCCcore easyconfig?!")


def post_prepare_hook(self, *args, **kwargs):
    """Main post-prepare hook: trigger custom functions."""

    if hasattr(self, EESSI_RPATH_OVERRIDE_ATTR):
        # Reset the value of 'rpath_override_dirs' now that we are finished with it
        update_build_option('rpath_override_dirs', getattr(self, EESSI_RPATH_OVERRIDE_ATTR))
        print_msg("Resetting rpath_override_dirs to original value: %s", getattr(self, EESSI_RPATH_OVERRIDE_ATTR))
        delattr(self, EESSI_RPATH_OVERRIDE_ATTR)

    if self.name in POST_PREPARE_HOOKS:
        POST_PREPARE_HOOKS[self.name](self, *args, **kwargs)


def parse_hook_casacore_disable_vectorize(ec, eprefix):
    """
    Disable 'vectorize' toolchain option for casacore 3.5.0 on aarch64/neoverse_v1
    Compiling casacore 3.5.0 with GCC 13.2.0 (foss-2023b) gives an error when building for aarch64/neoverse_v1.
    See also, https://github.com/EESSI/software-layer/pull/479
    """
    if ec.name == 'casacore':
        tcname, tcversion = ec['toolchain']['name'], ec['toolchain']['version']
        if (
            LooseVersion(ec.version) == LooseVersion('3.5.0') and
            tcname == 'foss' and tcversion == '2023b'
        ):
            cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
            if cpu_target == CPU_TARGET_NEOVERSE_V1:
                if not hasattr(ec, 'toolchainopts'):
                    ec['toolchainopts'] = {}
                ec['toolchainopts']['vectorize'] = False
                print_msg("Changed toochainopts for %s: %s", ec.name, ec['toolchainopts'])
            else:
                print_msg("Not changing option vectorize for %s on non-neoverse_v1", ec.name)
        else:
            print_msg("Not changing option vectorize for %s %s %s", ec.name, ec.version, ec.toolchain)
    else:
        raise EasyBuildError("casacore-specific hook triggered for non-casacore easyconfig?!")


def parse_hook_cgal_toolchainopts_precise(ec, eprefix):
    """Enable 'precise' rather than 'strict' toolchain option for CGAL on POWER."""
    if ec.name == 'CGAL':
        if get_cpu_architecture() == POWER:
            # 'strict' implies '-mieee-fp', which is not supported on POWER
            # see https://github.com/easybuilders/easybuild-framework/issues/2077
            ec['toolchainopts']['strict'] = False
            ec['toolchainopts']['precise'] = True
            print_msg("Tweaked toochainopts for %s: %s", ec.name, ec['toolchainopts'])
    else:
        raise EasyBuildError("CGAL-specific hook triggered for non-CGAL easyconfig?!")


def parse_hook_fontconfig_add_fonts(ec, eprefix):
    """Inject --with-add-fonts configure option for fontconfig."""
    if ec.name == 'fontconfig':
        # make fontconfig aware of fonts included with compat layer
        with_add_fonts = '--with-add-fonts=%s' % os.path.join(eprefix, 'usr', 'share', 'fonts')
        ec.update('configopts', with_add_fonts)
        print_msg("Added '%s' configure option for %s", with_add_fonts, ec.name)
    else:
        raise EasyBuildError("fontconfig-specific hook triggered for non-fontconfig easyconfig?!")


def parse_hook_gpaw_harcoded_path(ec, eprefix):
    # configure the siteconfig.py file + patch setup.py to prefix hardcoded /usr/* path with value of %(sysroot) template
    # (which will be empty if EasyBuild is not configured to use an alternate sysroot);
    if ec.name == 'GPAW':
        ec.update('preinstallopts', """echo "fftw = True" > siteconfig.py && """) 
        ec.update('preinstallopts', """echo "libvdwxc = True" >> siteconfig.py && """) 
        ec.update('preinstallopts', """echo "scalapack = True" >> siteconfig.py && """) 
        ec.update('preinstallopts', """echo "libraries = ['xc', 'fftw3', 'scalapack', 'vdwxc', 'elpa']" >>  siteconfig.py && """)
        ec.update('preinstallopts', """echo "extra_compile_args += ['-fopenmp']" >> siteconfig.py && """)
        ec.update('preinstallopts', """echo "extra_link_args += ['-fopenmp']" >> siteconfig.py && """)
        ec.update('preinstallopts', """sed -i 's@"/usr/@"%(sysroot)s/usr/@g' setup.py && """)
        print_msg("Using custom configure options for %s: %s", ec.name, ec['preinstallopts'])
    else:
        raise EasyBuildError("GPAW-specific hook triggered for non-GPAW easyconfig?!")


def parse_hook_grpcio_zlib(ec, ecprefix):
    if ec.name == 'grpcio' and ec.version in ['1.57.0']:
        exts_list = ec['exts_list']
        (exts_list[0][2])['preinstallopts'] = "sed -i 's@/usr@%(sysroot)s/usr@g' setup.py && export GRPC_PYTHON_BUILD_EXT_COMPILER_JOBS=%(parallel)s && export GRPC_PYTHON_CFLAGS=\"-fvisibility=hidden -fno-wrapv -fno-exceptions\" &&GRPC_PYTHON_BUILD_SYSTEM_OPENSSL=True GRPC_PYTHON_BUILD_SYSTEM_ZLIB=False GRPC_PYTHON_BUILD_SYSTEM_RE2=True GRPC_PYTHON_BUILD_SYSTEM_ABSL=True"
        print_msg("Modified the easyconfig to use compat ZLIB with GRPC_PYTHON_BUILD_SYSTEM_ZLIB=False")
    else:
        raise EasyBuildError("grpcio-specific hook triggered for a non-grpcio easyconfig?!")


def parse_hook_imagemagick_add_dependency(ec, eprefix):
    """Add dependency for PCRE/8.45 for ImageMagick/7.1.0-37"""
    if ec.name == 'ImageMagick':
        if LooseVersion(ec.version) == LooseVersion('7.1.0-37'):
            print_msg("Added dependency for PCRE/8.45 to %s/%s", ec.name, ec.version)
            ec['dependencies'].append(('PCRE', '8.45'))
        else:
            print_msg("Not adding dependency for PCRE/8.45 to %s/%s", ec.name, ec.version)
    else:
        raise EasyBuildError("ImageMagick-specific hook triggered for non-ImageMagick easyconfig?!")


def parse_hook_openblas_relax_lapack_tests_num_errors(ec, eprefix):
    """Relax number of failing numerical LAPACK tests for aarch64/* CPU targets for OpenBLAS < 0.3.23"""
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if ec.name == 'OpenBLAS':
        if LooseVersion(ec.version) < LooseVersion('0.3.23'):
            # relax maximum number of failed numerical LAPACK tests for aarch64/neoverse_v1 CPU target
            # since the default setting of 150 that works well on other aarch64 targets and x86_64 is a bit too strict
            # See https://github.com/EESSI/software-layer/issues/314
            cfg_option = 'max_failing_lapack_tests_num_errors'
            if cpu_target == CPU_TARGET_NEOVERSE_V1:
                orig_value = ec[cfg_option]
                ec[cfg_option] = 400
                print_msg("Maximum number of failing LAPACK tests with numerical errors for %s relaxed to %s (was %s)",
                          ec.name, ec[cfg_option], orig_value)
            elif cpu_target == CPU_TARGET_AARCH64_GENERIC:
                orig_value = ec[cfg_option]
                ec[cfg_option] = 302
                print_msg("Maximum number of failing LAPACK tests with numerical errors for %s relaxed to %s (was %s)",
                          ec.name, ec[cfg_option], orig_value)
            else:
                print_msg("Not changing option %s for %s on non-AARCH64", cfg_option, ec.name)
    else:
        raise EasyBuildError("OpenBLAS-specific hook triggered for non-OpenBLAS easyconfig?!")


def parse_hook_Pillow_SIMD_harcoded_paths(ec, eprefix):
    # patch setup.py to prefix hardcoded /usr/* and /lib paths with value of %(sysroot) template
    # (which will be empty if EasyBuild is not configured to use an alternate sysroot);
    # see also https://gitlab.com/eessi/support/-/issues/9
    if ec.name == 'Pillow-SIMD':
        ec.update('preinstallopts', """sed -i 's@"/usr/@"%(sysroot)s/usr/@g' setup.py && """)
        ec.update('preinstallopts', """sed -i 's@"/lib@"%(sysroot)s/lib@g' setup.py && """)
        print_msg("Using custom configure options for %s: %s", ec.name, ec['preinstallopts'])
    else:
        raise EasyBuildError("Pillow-SIMD-specific hook triggered for non-Pillow-SIMD easyconfig?!")


def parse_hook_pybind11_replace_catch2(ec, eprefix):
    """
    Replace Catch2 build dependency in pybind11 easyconfigs with one that doesn't use system toolchain.
    cfr. https://github.com/easybuilders/easybuild-easyconfigs/pull/19270
    """
    # this is mainly necessary to avoid that --missing keeps reporting Catch2/2.13.9 is missing,
    # and to avoid that we need to use "--from-pr 19270" for every easyconfigs that (indirectly) depends on pybind11
    if ec.name == 'pybind11' and ec.version in ['2.10.3', '2.11.1']:
        build_deps = ec['builddependencies']
        catch2_build_dep = None
        catch2_name, catch2_version = ('Catch2', '2.13.9')
        for idx, build_dep in enumerate(build_deps):
            if build_dep[0] == catch2_name and build_dep[1] == catch2_version:
                catch2_build_dep = build_dep
                break
        if catch2_build_dep and len(catch2_build_dep) == 4 and catch2_build_dep[3] == SYSTEM:
            build_deps[idx] = (catch2_name, catch2_version)


def parse_hook_qt5_check_qtwebengine_disable(ec, eprefix):
    """
    Disable check for QtWebEngine in Qt5 as workaround for problem with determining glibc version.
    """
    if ec.name == 'Qt5':
         # workaround for glibc version being reported as "UNKNOWN" in Gentoo Prefix environment by EasyBuild v4.7.2,
         # see also https://github.com/easybuilders/easybuild-framework/pull/4290
         ec['check_qtwebengine'] = False
         print_msg("Checking for QtWebEgine in Qt5 installation has been disabled")
    else:
        raise EasyBuildError("Qt5-specific hook triggered for non-Qt5 easyconfig?!")



def parse_hook_sentencepiece_disable_tcmalloc_aarch64(ec, eprefix):
    """
    Disable using TC_Malloc on 'aarch64/generic'
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if ec.name == 'SentencePiece' and ec.version in ['0.2.0']:
        if cpu_target == CPU_TARGET_AARCH64_GENERIC:
            print_msg("parse_hook for SentencePiece: OLD '%s'", ec['components'])
            new_components = []
            for item in ec['components']:
                if item[2]['easyblock'] == 'CMakeMake':
                    new_item = item[2]
                    new_item['configopts'] = '-DSPM_ENABLE_TCMALLOC=OFF'
                    new_components.append((item[0], item[1], new_item))
                else:
                    new_components.append(item)
            ec['components'] = new_components
            print_msg("parse_hook for SentencePiece: NEW '%s'", ec['components'])
        else:
            print_msg("parse_hook for SentencePiece on %s -> leaving configopts unchanged", cpu_target)
    else:
        raise EasyBuildError("SentencePiece-specific hook triggered for non-SentencePiece easyconfig?!")


def parse_hook_ucx_eprefix(ec, eprefix):
    """Make UCX aware of compatibility layer via additional configuration options."""
    if ec.name == 'UCX':
        ec.update('configopts', '--with-sysroot=%s' % eprefix)
        ec.update('configopts', '--with-rdmacm=%s' % os.path.join(eprefix, 'usr'))
        print_msg("Using custom configure options for %s: %s", ec.name, ec['configopts'])
    else:
        raise EasyBuildError("UCX-specific hook triggered for non-UCX easyconfig?!")


def parse_hook_lammps_remove_deps_for_CI_aarch64(ec, *args, **kwargs):
    """
    Remove x86_64 specific dependencies for the CI to pass on aarch64
    """
    if ec.name == 'LAMMPS' and ec.version in ('2Aug2023_update2',):
        if os.getenv('EESSI_CPU_FAMILY') == 'aarch64':
            # ScaFaCoS and tbb are not compatible with aarch64/* CPU targets,
            # so remove them as dependencies for LAMMPS (they're optional);
            # see also https://github.com/easybuilders/easybuild-easyconfigs/pull/19164 +
            # https://github.com/easybuilders/easybuild-easyconfigs/pull/19000;
            # we need this hook because we check for missing installations for all CPU targets
            # on an x86_64 VM in GitHub Actions (so condition based on ARCH in LAMMPS easyconfig is always true)
            ec['dependencies'] = [dep for dep in ec['dependencies'] if dep[0] not in ('ScaFaCoS', 'tbb')]
            # if optarch is GENERIC, we also set 'kokkos_arch' to 'ARMV80'
            #   if not set the easyblock will run
            #   "python -c 'from archspec.cpu import host; print(host())'"
            #   which returns the host architecture which is then mapped to some
            #   identifier corresponding to the architecture;
            #   however, this may not be correct if we want to build for
            #   `aarch64/generic`
            if build_option('optarch') == OPTARCH_GENERIC:
                ec['kokkos_arch'] = 'ARMV80'
                print_msg("Set kokkos_arch = 'ARMV80' (cpu family: %s, optarch: %s)",
                    os.getenv('EESSI_CPU_FAMILY'), build_option('optarch'))
    else:
        raise EasyBuildError("LAMMPS-specific hook triggered for non-LAMMPS easyconfig?!")


def parse_hook_CP2K_remove_deps_for_aarch64(ec, *args, **kwargs):
    """
    Remove x86_64 specific dependencies for the CI and missing installations to pass on aarch64
    """
    if ec.name == 'CP2K' and ec.version in ('2023.1',):
        if os.getenv('EESSI_CPU_FAMILY') == 'aarch64':
            # LIBXSMM is not supported on ARM with GCC 12.2.0 and 12.3.0
            # See https://www.cp2k.org/dev:compiler_support
            # See https://github.com/easybuilders/easybuild-easyconfigs/pull/20951
            # we need this hook because we check for missing installations for all CPU targets
            # on an x86_64 VM in GitHub Actions (so condition based on ARCH in LAMMPS easyconfig is always true)
            ec['dependencies'] = [dep for dep in ec['dependencies'] if dep[0] not in ('libxsmm',)]
    else:
        raise EasyBuildError("CP2K-specific hook triggered for non-CP2K easyconfig?!")


def parse_hook_librosa_custom_ctypes(ec, *args, **kwargs):
    """
    Add exts_filter to soundfile extension in exts_list
    """
    if ec.name == 'librosa' and ec.version in ('0.10.1',):
        ec_dict = ec.asdict()
        eessi_software_path = get_eessi_envvar('EESSI_SOFTWARE_PATH')
        custom_ctypes_path = os.path.join(eessi_software_path, "software", "custom_ctypes", "1.2")
        ebpythonprefixes = "EBPYTHONPREFIXES=%s" % custom_ctypes_path
        exts_list_new = []
        for item in ec_dict['exts_list']:
            if item[0] == 'soundfile':
                ext_dict = item[2]
                ext_dict['exts_filter'] = (ebpythonprefixes + ' ' + EXTS_FILTER_PYTHON_PACKAGES[0],
                                           EXTS_FILTER_PYTHON_PACKAGES[1])
                exts_list_new.append((item[0], item[1], ext_dict))
            else:
                exts_list_new.append(item)
        ec['exts_list'] = exts_list_new
        print_msg("New exts_list: '%s'", ec['exts_list'])
    else:
        raise EasyBuildError("librosa/0.10.1-specific hook triggered for non-librosa/0.10.1 easyconfig?!")


def pre_prepare_hook_highway_handle_test_compilation_issues(self, *args, **kwargs):
    """
    Solve issues with compiling or running the tests on both
    neoverse_n1 and neoverse_v1 with Highway 1.0.4 and GCC 12.3.0:
      - for neoverse_n1 we set optarch to GENERIC
      - for neoverse_v1 we completely disable the tests
    cfr. https://github.com/EESSI/software-layer/issues/469
    """
    if self.name == 'Highway':
        tcname, tcversion = self.toolchain.name, self.toolchain.version
        cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
        # note: keep condition in sync with the one used in 
        # post_prepare_hook_highway_handle_test_compilation_issues
        if self.version in ['1.0.4'] and tcname == 'GCCcore' and tcversion == '12.3.0':
            if cpu_target == CPU_TARGET_NEOVERSE_V1:
                self.cfg.update('configopts', '-DHWY_ENABLE_TESTS=OFF')
            if cpu_target == CPU_TARGET_NEOVERSE_N1:
                self.orig_optarch = build_option('optarch')
                update_build_option('optarch', OPTARCH_GENERIC)
    else:
        raise EasyBuildError("Highway-specific hook triggered for non-Highway easyconfig?!")


def post_prepare_hook_highway_handle_test_compilation_issues(self, *args, **kwargs):
    """
    Post-prepare hook for Highway to reset optarch build option.
    """
    if self.name == 'Highway':
        tcname, tcversion = self.toolchain.name, self.toolchain.version
        cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
        # note: keep condition in sync with the one used in 
        # pre_prepare_hook_highway_handle_test_compilation_issues
        if self.version in ['1.0.4'] and tcname == 'GCCcore' and tcversion == '12.3.0':
            if cpu_target == CPU_TARGET_NEOVERSE_N1:
                update_build_option('optarch', self.orig_optarch)

def pre_configure_hook(self, *args, **kwargs):
    """Main pre-configure hook: trigger custom functions based on software name."""
    if self.name in PRE_CONFIGURE_HOOKS:
        PRE_CONFIGURE_HOOKS[self.name](self, *args, **kwargs)


def pre_configure_hook_gobject_introspection(self, *args, **kwargs):
    """
    pre-configure hook for GObject-Introspection:
    - prevent GObject-Introspection from setting $LD_LIBRARY_PATH if EasyBuild is configured to filter it, see:
      https://github.com/EESSI/software-layer/issues/196
    """
    if self.name == 'GObject-Introspection':
        # inject a line that removes all items from runtime_path_envvar that are in $EASYBUILD_FILTER_ENVVARS
        sed_cmd = r'sed -i "s@\(^\s*runtime_path_envvar = \)\(.*\)@'
        sed_cmd += r'\1\2\n\1 [x for x in runtime_path_envvar if not x in os.environ.get(\'EASYBUILD_FILTER_ENV_VARS\', \'\').split(\',\')]@g"'
        sed_cmd += ' %(start_dir)s/giscanner/ccompiler.py && '
        self.cfg.update('preconfigopts', sed_cmd)
    else:
        raise EasyBuildError("GObject-Introspection-specific hook triggered for non-GObject-Introspection easyconfig?!")


def pre_configure_hook_gromacs(self, *args, **kwargs):
    """
    Pre-configure hook for GROMACS:
    - avoid building with SVE instructions on Neoverse V1 as workaround for failing tests,
      see https://gitlab.com/gromacs/gromacs/-/issues/5057 + https://gitlab.com/eessi/support/-/issues/47
    """
    if self.name == 'GROMACS':
        cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
        if LooseVersion(self.version) <= LooseVersion('2024.1') and cpu_target == CPU_TARGET_NEOVERSE_V1:
            self.cfg.update('configopts', '-DGMX_SIMD=ARM_NEON_ASIMD')
            print_msg("Avoiding use of SVE instructions for GROMACS %s by using ARM_NEON_ASIMD as GMX_SIMD value", self.version)
    else:
        raise EasyBuildError("GROMACS-specific hook triggered for non-GROMACS easyconfig?!")


def pre_configure_hook_openblas_optarch_generic(self, *args, **kwargs):
    """
    Pre-configure hook for OpenBLAS: add DYNAMIC_ARCH=1 to build/test/install options when using --optarch=GENERIC
    """
    if self.name == 'OpenBLAS':
        if build_option('optarch') == OPTARCH_GENERIC:
            for step in ('build', 'test', 'install'):
                self.cfg.update(f'{step}opts', "DYNAMIC_ARCH=1")

            # use -mtune=generic rather than -mcpu=generic in $CFLAGS on aarch64,
            # because -mcpu=generic implies a particular -march=armv* which clashes with those used by OpenBLAS
            # when building with DYNAMIC_ARCH=1
            if get_cpu_architecture() == AARCH64:
                cflags = os.getenv('CFLAGS').replace('-mcpu=generic', '-mtune=generic')
                env.setvar('CFLAGS', cflags)
    else:
        raise EasyBuildError("OpenBLAS-specific hook triggered for non-OpenBLAS easyconfig?!")


def pre_configure_hook_pytorch_add_cupti_libdir(self, *args, **kwargs):
    """
    Pre-configure hook for PyTorch: add directory $EESSI_SOFTWARE_PATH/software/CUDA/12.1.1/extras/CUPTI/lib64 to LIBRARY_PATH
    """
    if self.name == 'PyTorch':
        if 'cudaver' in self.cfg.template_values and self.cfg.template_values['cudaver'] == '12.1.1':
            _cudaver = self.cfg.template_values['cudaver']
            print_msg("pre_configure_hook_pytorch_add_cupti_libdir: CUDA version: '%s'" % _cudaver)
            _library_path = os.getenv('LIBRARY_PATH')
            print_msg("pre_configure_hook_pytorch_add_cupti_libdir: library_path: '%s'", _library_path)
            _eessi_software_path = os.getenv('EESSI_SOFTWARE_PATH')
            print_msg("pre_configure_hook_pytorch_add_cupti_libdir: eessi_software_path: '%s'", _eessi_software_path)
            _cupti_lib_dir = os.path.join(_eessi_software_path, 'software', 'CUDA', _cudaver, 'extras', 'CUPTI', 'lib64')
            print_msg("pre_configure_hook_pytorch_add_cupti_libdir: cupti_lib_dir: '%s'", _cupti_lib_dir)
            if _library_path:
                env.setvar('LIBRARY_PATH', ':'.join([_library_path, _cupti_lib_dir]))
            else:
                env.setvar('LIBRARY_PATH', _cupti_lib_dir)
            print_msg("pre_configure_hook_pytorch_add_cupti_libdir: LIBRARY_PATH: '%s'", os.getenv('LIBRARY_PATH'))
    else:
        raise EasyBuildError("PyTorch-specific hook triggered for non-PyTorch easyconfig?!")


def pre_configure_hook_libfabric_disable_psm3_x86_64_generic(self, *args, **kwargs):
    """Add --disable-psm3 to libfabric configure options when building with --optarch=GENERIC on x86_64."""
    if self.name == 'libfabric':
        if get_cpu_architecture() == X86_64:
            generic = build_option('optarch') == OPTARCH_GENERIC
            no_avx = 'avx' not in get_cpu_features()
            if generic or no_avx:
                self.cfg.update('configopts', '--disable-psm3')
                print_msg("Using custom configure options for %s: %s", self.name, self.cfg['configopts'])
    else:
        raise EasyBuildError("libfabric-specific hook triggered for non-libfabric easyconfig?!")


def pre_configure_hook_metabat_filtered_zlib_dep(self, *args, **kwargs):
    """
    Pre-configure hook for MetaBAT:
    - take into account that zlib is a filtered dependency,
      and that there's no libz.a in the EESSI compat layer
    """
    if self.name == 'MetaBAT':
        configopts = self.cfg['configopts']
        regex = re.compile(r"\$EBROOTZLIB/lib/libz.a")
        self.cfg['configopts'] = regex.sub('$EPREFIX/usr/lib64/libz.so', configopts)
    else:
        raise EasyBuildError("MetaBAT-specific hook triggered for non-MetaBAT easyconfig?!")


def pre_configure_hook_wrf_aarch64(self, *args, **kwargs):
    """
    Pre-configure hook for WRF:
    - patch arch/configure_new.defaults so building WRF with foss toolchain works on aarch64
    """
    if self.name == 'WRF':
        if get_cpu_architecture() == AARCH64:
            pattern = "Linux x86_64 ppc64le, gfortran"
            repl = "Linux x86_64 aarch64 ppc64le, gfortran"
            if LooseVersion(self.version) <= LooseVersion('3.9.0'):
                    self.cfg.update('preconfigopts', "sed -i 's/%s/%s/g' arch/configure_new.defaults && " % (pattern, repl))
                    print_msg("Using custom preconfigopts for %s: %s", self.name, self.cfg['preconfigopts'])

            if LooseVersion('4.0.0') <= LooseVersion(self.version) <= LooseVersion('4.2.1'):
                    self.cfg.update('preconfigopts', "sed -i 's/%s/%s/g' arch/configure.defaults && " % (pattern, repl))
                    print_msg("Using custom preconfigopts for %s: %s", self.name, self.cfg['preconfigopts'])
    else:
        raise EasyBuildError("WRF-specific hook triggered for non-WRF easyconfig?!")


def pre_test_hook(self,*args, **kwargs):
    """Main pre-test hook: trigger custom functions based on software name."""
    if self.name in PRE_TEST_HOOKS:
        PRE_TEST_HOOKS[self.name](self, *args, **kwargs)


def pre_test_hook_exclude_failing_test_Highway(self, *args, **kwargs):
    """
    Pre-test hook for Highway: exclude failing TestAllShiftRightLanes/SVE_256 test on neoverse_v1
    cfr. https://github.com/EESSI/software-layer/issues/469
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if self.name == 'Highway' and self.version in ['1.0.3'] and cpu_target == CPU_TARGET_NEOVERSE_V1:
        self.cfg['runtest'] += ' ARGS="-E TestAllShiftRightLanes/SVE_256"'


def pre_test_hook_ignore_failing_tests_ESPResSo(self, *args, **kwargs):
    """
    Pre-test hook for ESPResSo: skip failing tests, tests frequently timeout due to known bugs in ESPResSo v4.2.1
    cfr. https://github.com/EESSI/software-layer/issues/363
    """
    if self.name == 'ESPResSo' and self.version == '4.2.1':
        self.cfg['testopts'] = "|| echo 'ignoring failing tests (probably due to timeouts)'"


def pre_test_hook_ignore_failing_tests_FFTWMPI(self, *args, **kwargs):
    """
    Pre-test hook for FFTW.MPI: skip failing tests for FFTW.MPI 3.3.10 on neoverse_v1
    cfr. https://github.com/EESSI/software-layer/issues/325
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if self.name == 'FFTW.MPI' and self.version == '3.3.10' and cpu_target == CPU_TARGET_NEOVERSE_V1:
        self.cfg['testopts'] = "|| echo ignoring failing tests"


def pre_test_hook_ignore_failing_tests_SciPybundle(self, *args, **kwargs):
    """
    Pre-test hook for SciPy-bundle: skip failing tests for selected SciPy-bundle versions
    In version 2021.10, 2 failing tests in scipy 1.6.3:
        FAILED optimize/tests/test_linprog.py::TestLinprogIPSparse::test_bug_6139 - A...
        FAILED optimize/tests/test_linprog.py::TestLinprogIPSparsePresolve::test_bug_6139
        = 2 failed, 30554 passed, 2064 skipped, 10992 deselected, 76 xfailed, 7 xpassed, 40 warnings in 380.27s (0:06:20) =
    In versions 2023.02, 2023.07, and 2023.11, 2 failing tests in scipy (versions 1.10.1, 1.11.1, 1.11.4):
        FAILED scipy/spatial/tests/test_distance.py::TestPdist::test_pdist_correlation_iris
        FAILED scipy/spatial/tests/test_distance.py::TestPdist::test_pdist_correlation_iris_float32
        = 2 failed, 54409 passed, 3016 skipped, 223 xfailed, 13 xpassed, 10917 warnings in 892.04s (0:14:52) =
    In previous versions we were not as strict yet on the numpy/SciPy tests
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    scipy_bundle_versions = ('2021.10', '2023.02', '2023.07', '2023.11')
    if self.name == 'SciPy-bundle' and self.version in scipy_bundle_versions and cpu_target == CPU_TARGET_NEOVERSE_V1:
        self.cfg['testopts'] = "|| echo ignoring failing tests"

def pre_test_hook_ignore_failing_tests_netCDF(self, *args, **kwargs):
    """
    Pre-test hook for netCDF: skip failing tests for selected netCDF versions on neoverse_v1
    cfr. https://github.com/EESSI/software-layer/issues/425
    The following tests are problematic:
        163 - nc_test4_run_par_test (Timeout)
        190 - h5_test_run_par_tests (Timeout)
    A few other tests are skipped in the easyconfig and patches for similar issues, see above issue for details.
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if self.name == 'netCDF' and self.version == '4.9.2' and cpu_target == CPU_TARGET_NEOVERSE_V1:
        self.cfg['testopts'] = "|| echo ignoring failing tests"

def pre_test_hook_increase_max_failed_tests_arm_PyTorch(self, *args, **kwargs):
    """
    Pre-test hook for PyTorch: increase max failing tests for ARM for PyTorch 2.1.2
    See https://github.com/EESSI/software-layer/pull/444#issuecomment-1890416171
    """
    if self.name == 'PyTorch' and self.version == '2.1.2' and get_cpu_architecture() == AARCH64:
        self.cfg['max_failed_tests'] = 10
        if 'cudaver' in self.cfg.template_values and self.cfg.template_values['cudaver'] == '12.1.1':
            _cudaver = self.cfg.template_values['cudaver']
            _runtest = self.cfg['runtest']
            self.cfg['runtest'] = _runtest.replace(
                                      'PYTHONUNBUFFERED',
                                      'PYTORCH_TEST_RUN_EVERYTHING_IN_SERIAL=1 PYTHONUNBUFFERED')


def pre_single_extension_hook(ext, *args, **kwargs):
    """Main pre-extension: trigger custom functions based on software name."""
    if ext.name in PRE_SINGLE_EXTENSION_HOOKS:
        PRE_SINGLE_EXTENSION_HOOKS[ext.name](ext, *args, **kwargs)


def post_single_extension_hook(ext, *args, **kwargs):
    """Main post-extension hook: trigger custom functions based on software name."""
    if ext.name in POST_SINGLE_EXTENSION_HOOKS:
        POST_SINGLE_EXTENSION_HOOKS[ext.name](ext, *args, **kwargs)


def pre_single_extension_isoband(ext, *args, **kwargs):
    """
    Pre-extension hook for isoband R package, to fix build on top of recent glibc.
    """
    if ext.name == 'isoband' and LooseVersion(ext.version) < LooseVersion('0.2.5'):
        # use constant value instead of SIGSTKSZ for stack size in vendored testthat included in isoband sources,
        # cfr. https://github.com/r-lib/isoband/commit/6984e6ce8d977f06e0b5ff73f5d88e5c9a44c027
        ext.cfg['preinstallopts'] = "sed -i 's/SIGSTKSZ/32768/g' src/testthat/vendor/catch.h && "


def pre_single_extension_numpy(ext, *args, **kwargs):
    """
    Pre-extension hook for numpy, to change -march=native to -march=armv8.4-a for numpy 1.24.2
    when building for aarch64/neoverse_v1 CPU target.
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if ext.name == 'numpy' and ext.version == '1.24.2' and cpu_target == CPU_TARGET_NEOVERSE_V1:
        # note: this hook is called before build environment is set up (by calling toolchain.prepare()),
        # so environment variables like $CFLAGS are not defined yet
        # unsure which of these actually matter for numpy, so changing all of them
        ext.orig_optarch = build_option('optarch')
        update_build_option('optarch', 'march=armv8.4-a')


def post_single_extension_numpy(ext, *args, **kwargs):
    """
    Post-extension hook for numpy, to reset 'optarch' build option.
    """
    cpu_target = get_eessi_envvar('EESSI_SOFTWARE_SUBDIR')
    if ext.name == 'numpy' and ext.version == '1.24.2' and cpu_target == CPU_TARGET_NEOVERSE_V1:
        update_build_option('optarch', ext.orig_optarch)


def pre_single_extension_testthat(ext, *args, **kwargs):
    """
    Pre-extension hook for testthat R package, to fix build on top of recent glibc.
    """
    if ext.name == 'testthat' and LooseVersion(ext.version) < LooseVersion('3.1.0'):
        # use constant value instead of SIGSTKSZ for stack size,
        # cfr. https://github.com/r-lib/testthat/issues/1373 + https://github.com/r-lib/testthat/pull/1403
        ext.cfg['preinstallopts'] = "sed -i 's/SIGSTKSZ/32768/g' inst/include/testthat/vendor/catch.h && "


def post_sanitycheck_hook(self, *args, **kwargs):
    """Main post-sanity-check hook: trigger custom functions based on software name."""
    if self.name in POST_SANITYCHECK_HOOKS:
        POST_SANITYCHECK_HOOKS[self.name](self, *args, **kwargs)


def replace_non_distributable_files_with_symlinks(log, install_dir, package, allowlist):
    """
    Replace files that cannot be distributed with symlinks into host_injections
    """
    extension_based = { "CUDA": False, "cuDNN": True, "cuTENSOR": True }
    if not package in extension_based:
        raise EasyBuildError("Don't know how to strip non-distributable files from package %s.", package)

    # iterate over all files in the package installation directory
    for dir_path, _, files in os.walk(install_dir):
        for filename in files:
            full_path = os.path.join(dir_path, filename)
            # we only really care about real files, i.e. not symlinks
            if not os.path.islink(full_path):
                # check if the current file name stub is part of the allowlist
                basename =  filename.split('.')[0]
                if extension_based[package]:
                    if '.' in filename:
                        extension = '.' + filename.split('.')[1]
                if basename in allowlist:
                    log.debug("%s is found in allowlist, so keeping it: %s", basename, full_path)
                elif extension_based[package] and '.' in filename and extension in allowlist:
                    log.debug("%s is found in allowlist, so keeping it: %s", extension, full_path)
                else:
                    if extension_based[package]:
                        print_name = filename
                    else:
                        print_name = basename
                    log.debug("%s is not found in allowlist, so replacing it with symlink: %s",
                              print_name, full_path)
                    # if it is not in the allowlist, delete the file and create a symlink to host_injections
                    host_inj_path = full_path.replace('versions', 'host_injections')
                    # make sure source and target of symlink are not the same
                    if full_path == host_inj_path:
                        raise EasyBuildError("Source (%s) and target (%s) are the same location, are you sure you "
                                             "are using this hook for an EESSI installation?",
                                             full_path, host_inj_path)
                    remove_file(full_path)
                    symlink(host_inj_path, full_path)


def post_sanitycheck_cuda(self, *args, **kwargs):
    """
    Remove files from CUDA installation that we are not allowed to ship,
    and replace them with a symlink to a corresponding installation under host_injections.
    """
    if self.name == 'CUDA':
        print_msg("Replacing files in CUDA installation that we can not ship with symlinks to host_injections...")

        # read CUDA EULA, construct allowlist based on section 2.6 that specifies list of files that can be shipped
        eula_path = os.path.join(self.installdir, 'EULA.txt')
        relevant_eula_lines = []
        with open(eula_path) as infile:
            copy = False
            for line in infile:
                if line.strip() == "2.6. Attachment A":
                    copy = True
                    continue
                elif line.strip() == "2.7. Attachment B":
                    copy = False
                    continue
                elif copy:
                    relevant_eula_lines.append(line)

        # create list without file extensions, they're not really needed and they only complicate things
        allowlist = ['EULA', 'README']
        file_extensions = ['.so', '.a', '.h', '.bc']
        for line in relevant_eula_lines:
            for word in line.split():
                if any(ext in word for ext in file_extensions):
                    allowlist.append(os.path.splitext(word)[0])
        allowlist = sorted(set(allowlist))
        self.log.info("Allowlist for files in CUDA installation that can be redistributed: " + ', '.join(allowlist))

        # Do some quick sanity checks for things we should or shouldn't have in the list
        if 'nvcc' in allowlist:
            raise EasyBuildError("Found 'nvcc' in allowlist: %s" % allowlist)
        if 'libcudart' not in allowlist:
            raise EasyBuildError("Did not find 'libcudart' in allowlist: %s" % allowlist)

        # replace files that are not distributable with symlinks into
        # host_injections
        replace_non_distributable_files_with_symlinks(self.log, self.installdir, self.name, allowlist)
    else:
        raise EasyBuildError("CUDA-specific hook triggered for non-CUDA easyconfig?!")


def post_sanitycheck_cudnn(self, *args, **kwargs):
    """
    Remove files from cuDNN installation that we are not allowed to ship,
    and replace them with a symlink to a corresponding installation under host_injections.
    """
    if self.name == 'cuDNN':
        print_msg("Replacing files in cuDNN installation that we can not ship with symlinks to host_injections...")

        allowlist = ['LICENSE']

        # read cuDNN LICENSE, construct allowlist based on section "2. Distribution" that specifies list of files that can be shipped
        license_path = os.path.join(self.installdir, 'LICENSE')
        search_string = "2. Distribution. The following portions of the SDK are distributable under the Agreement:"
        with open(license_path) as infile:
            for line in infile:
                if line.strip().startswith(search_string):
                    # remove search string, split into words, remove trailing
                    # dots '.' and only retain words starting with a dot '.'
                    distributable = line[len(search_string):]
                    for word in distributable.split():
                        if word[0] == '.':
                            allowlist.append(word.rstrip('.'))

        allowlist = sorted(set(allowlist))
        self.log.info("Allowlist for files in cuDNN installation that can be redistributed: " + ', '.join(allowlist))

        # replace files that are not distributable with symlinks into
        # host_injections
        replace_non_distributable_files_with_symlinks(self.log, self.installdir, self.name, allowlist)
    else:
        raise EasyBuildError("cuDNN-specific hook triggered for non-cuDNN easyconfig?!")


def post_sanitycheck_cutensor(self, *args, **kwargs):
    """
    Remove files from cuTENSOR installation that we are not allowed to ship,
    and replace them with a symlink to a corresponding installation under host_injections.
    """
    if self.name == 'cuTENSOR':
        print_msg("Replacing files in cuTENSOR installation that we can not ship with symlinks to host_injections...")

        allowlist = ['LICENSE']

        # read cuTENSOR LICENSE, construct allowlist based on section "2. Distribution" that specifies list of files that can be shipped
        license_path = os.path.join(self.installdir, 'LICENSE')
        search_string = "2. Distribution. The following portions of the SDK are distributable under the Agreement:"
        with open(license_path) as infile:
            for line in infile:
                if line.strip().startswith(search_string):
                    # remove search string, split into words, remove trailing
                    # dots '.' and only retain words starting with a dot '.'
                    distributable = line[len(search_string):]
                    for word in distributable.split():
                        if word[0] == '.':
                            allowlist.append(word.rstrip('.'))

        allowlist = sorted(set(allowlist))
        self.log.info("Allowlist for files in cuTENSOR installation that can be redistributed: " + ', '.join(allowlist))

        # replace files that are not distributable with symlinks into
        # host_injections
        replace_non_distributable_files_with_symlinks(self.log, self.installdir, self.name, allowlist)
    else:
        raise EasyBuildError("cuTENSOR-specific hook triggered for non-cuTENSOR easyconfig?!")


def inject_gpu_property(ec):
    """
    Add 'gpu' property EESSI<PACKAGE>VERSION envvars and drop dependencies to
    build dependencies, via modluafooter easyconfig parameter
    """
    ec_dict = ec.asdict()
    # check if CUDA, cuDNN, you-name-it is in the dependencies, if so
    # - drop dependency to build dependency
    # - add 'gpu' Lmod property
    # - add envvar with package version
    packages_list = ( "CUDA", "cuDNN", "cuTENSOR" )
    packages_version = { }
    add_gpu_property = ''

    for package in packages_list:
        # Check if package is in the dependencies, if so drop dependency to build
        # dependency and set variable for later adding the 'gpu' Lmod property
        if (package in [dep[0] for dep in iter(ec_dict['dependencies'])]):
            add_gpu_property = 'add_property("arch","gpu")'
            for dep in iter(ec_dict['dependencies']):
                if package in dep[0]:
                    # make package a build dependency only (rpathing saves us from link errors)
                    ec.log.info("Dropping dependency on %s to build dependency" % package)
                    ec_dict['dependencies'].remove(dep)
                    if dep not in ec_dict['builddependencies']:
                        ec_dict['builddependencies'].append(dep)
                    # take note of version for creating the modluafooter
                    packages_version[package] = dep[1]
    if add_gpu_property:
        ec.log.info("Injecting gpu as Lmod arch property and envvars for dependencies with their version")
        key = 'modluafooter'
        values = [add_gpu_property]
        for package, version in packages_version.items():
            envvar = "EESSI%sVERSION" % package.upper()
            values.append('setenv("%s","%s")' % (envvar, version))
        if not key in ec_dict:
            ec[key] = '\n'.join(values)
        else:
            new_value = ec_dict[key]
            for value in values:
                if not value in new_value:
                    new_value = '\n'.join([new_value, value])
            ec[key] = new_value

    return ec



def pre_module_hook(self, *args, **kwargs):
    """Main pre-module-check hook: trigger custom functions based on software name."""
    if self.name in PRE_MODULE_HOOKS:
        PRE_MODULE_HOOKS[self.name](self, *args, **kwargs)


def pre_module_hook_librosa_augment_modluafooter(self, *args, **kwargs):
    """
    Add EBPYTHONPREFIXES to modluafooter
    """
    if self.name == 'librosa' and self.version == '0.10.1':
        eessi_software_path = get_eessi_envvar('EESSI_SOFTWARE_PATH')
        custom_ctypes_path = os.path.join(eessi_software_path, "software", "custom_ctypes", "1.2")
        key = 'modluafooter'
        values = ['prepend_path("EBPYTHONPREFIXES","%s")' % (custom_ctypes_path)]
        print_msg("Adding '%s' to modluafooter", values[0])
        if not key in self.cfg:
            self.cfg[key] = '\n'.join(values)
        else:
            new_value = self.cfg[key]
            for value in values:
                if not value in new_value:
                    new_value = '\n'.join([new_value, value])
            self.cfg[key] = new_value
        print_msg("Full modluafooter is '%s'", self.cfg[key])
    else:
        raise EasyBuildError("librosa/0.10.1-specific hook triggered for non-librosa/0.10.1 easyconfig?!")


PARSE_HOOKS = {
    'casacore': parse_hook_casacore_disable_vectorize,
    'CGAL': parse_hook_cgal_toolchainopts_precise,
    'fontconfig': parse_hook_fontconfig_add_fonts,
    'GPAW': parse_hook_gpaw_harcoded_path,
    'grpcio': parse_hook_grpcio_zlib,
    'ImageMagick': parse_hook_imagemagick_add_dependency,
    'LAMMPS': parse_hook_lammps_remove_deps_for_CI_aarch64,
    'librosa': parse_hook_librosa_custom_ctypes,
    'CP2K': parse_hook_CP2K_remove_deps_for_aarch64,
    'OpenBLAS': parse_hook_openblas_relax_lapack_tests_num_errors,
    'Pillow-SIMD' : parse_hook_Pillow_SIMD_harcoded_paths,
    'pybind11': parse_hook_pybind11_replace_catch2,
    'Qt5': parse_hook_qt5_check_qtwebengine_disable,
    'SentencePiece': parse_hook_sentencepiece_disable_tcmalloc_aarch64,
    'UCX': parse_hook_ucx_eprefix,
}

PRE_PREPARE_HOOKS = {
    'Highway': pre_prepare_hook_highway_handle_test_compilation_issues,
}

POST_PREPARE_HOOKS = {
    'GCCcore': post_prepare_hook_gcc_prefixed_ld_rpath_wrapper,
    'Highway': post_prepare_hook_highway_handle_test_compilation_issues,
}

PRE_CONFIGURE_HOOKS = {
    'GObject-Introspection': pre_configure_hook_gobject_introspection,
    'GROMACS': pre_configure_hook_gromacs,
    'libfabric': pre_configure_hook_libfabric_disable_psm3_x86_64_generic,
    'MetaBAT': pre_configure_hook_metabat_filtered_zlib_dep,
    'OpenBLAS': pre_configure_hook_openblas_optarch_generic,
    'PyTorch': pre_configure_hook_pytorch_add_cupti_libdir,
    'WRF': pre_configure_hook_wrf_aarch64,
}

PRE_TEST_HOOKS = {
    'ESPResSo': pre_test_hook_ignore_failing_tests_ESPResSo,
    'FFTW.MPI': pre_test_hook_ignore_failing_tests_FFTWMPI,
    'Highway': pre_test_hook_exclude_failing_test_Highway,
    'SciPy-bundle': pre_test_hook_ignore_failing_tests_SciPybundle,
    'netCDF': pre_test_hook_ignore_failing_tests_netCDF,
    'PyTorch': pre_test_hook_increase_max_failed_tests_arm_PyTorch,
}

PRE_SINGLE_EXTENSION_HOOKS = {
    'isoband': pre_single_extension_isoband,
    'numpy': pre_single_extension_numpy,
    'testthat': pre_single_extension_testthat,
}

POST_SINGLE_EXTENSION_HOOKS = {
    'numpy': post_single_extension_numpy,
}

POST_SANITYCHECK_HOOKS = {
    'CUDA': post_sanitycheck_cuda,
    'cuDNN': post_sanitycheck_cudnn,
    'cuTENSOR': post_sanitycheck_cutensor,
}

PRE_MODULE_HOOKS = {
    'librosa': pre_module_hook_librosa_augment_modluafooter,
}
