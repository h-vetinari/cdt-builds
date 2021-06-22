import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import collections
import glob

import tqdm
import click
from ruamel.yaml import YAML

from cdt_config import (
    LEGACY_CDT_PATH,
    LEGACY_CUSTOM_CDT_PATH,
    CDT_PATH,
    CUSTOM_CDT_PATH,
)
from render_readme import render_readme


def _ignore_url_build_changes(base_dir):
    print("fixing build url only changes for path '%s'..." % base_dir)
    line_slugs = [
        "-  - url:",
        "-    sha256:",
        "-  # - url:",
    ]

    cdts = glob.glob(os.path.join(base_dir, "*"))
    for cdt in cdts:
        fnames = glob.glob(os.path.join(cdt, "*"))
        changed_files = set()
        for fname in fnames:
            if _is_changed_or_not_tracked(fname):
                changed_files.add(fname)

        if (
            len(changed_files) == 1
            and set([os.path.basename(f) for f in changed_files]) == set(["meta.yaml"])
        ):
            print("    cdt:", cdt)
            diff = subprocess.run(
                "git diff %s" % fname,
                shell=True,
                capture_output=True,
            )
            diff_lines = []
            start = False
            for line in diff.stdout.decode("utf-8").splitlines():
                if line.startswith("@@"):
                    start = True
                if start:
                    diff_lines.append(line)

            print("        diff:", "\n        " + "\n        ".join(diff_lines))
            bad_line = False
            for line in diff_lines:
                if (
                    line.startswith("-")
                    and not any([line.startswith(s) for s in line_slugs])
                ):
                    bad_line = True
                    break

            if not bad_line:
                print("        rolling back changes")
                subprocess.run(
                    "git co -- %s" % fname,
                    shell=True,
                )


def _is_changed_or_not_tracked(pth):
    ctracked = subprocess.run(
        "git ls-files --error-unmatch %s" % pth,
        shell=True,
        capture_output=True,
    )
    if ctracked.returncode != 0:
        return True
    else:
        cdiff = subprocess.run(
            "git diff --exit-code -s %s" % pth,
            shell=True,
            capture_output=True,
        )

        if cdiff.returncode != 0:
            return True
        else:
            return False


def _gen_dist_arch_str(arch, dist):
    return "%s-%s" % (dist.replace("ent", ""), arch)


def _make_cdt_recipes(*, extra, cdt_path, arch_dist_tuples, cdts, exec, force):
    futures = {}
    for arch, dist in arch_dist_tuples:
        for cdt, cfg in cdts.items():
            if cfg["custom"]:
                continue

            if (
                "skipped_cdts" in cfg
                and _gen_dist_arch_str(arch, dist) in cfg["skipped_cdts"]
            ):
                continue

            if cfg.get("recursive", True):
                _extra = extra + " --recursive"
            else:
                _extra = extra

            _pth = os.path.join(
                cdt_path,
                cdt.lower() + "-" + _gen_dist_arch_str(arch, dist),
            )

            if not force and os.path.exists(_pth):
                continue

            futures[exec.submit(
                subprocess.run,
                (
                    f"python rpm.py {cdt} --output-dir={cdt_path} "
                    + f"--architecture={arch} --distro={dist} "
                    + _extra
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=True
            )] = {"cdt": cdt, "arch": arch, "dist": dist}
    return futures


def _cleanup_custom_cdt_overlaps(*, cdt_path, arch_dist_tuples, cdts):
    for arch, dist in arch_dist_tuples:
        for cdt, cfg in cdts.items():
            if not cfg["custom"]:
                continue

            pth = os.path.join(
                cdt_path,
                cdt.lower() + "-" + dist.replace("ent", "") + "-" + arch,
            )
            if os.path.exists(pth):
                try:
                    subprocess.run(
                        "git rm -r -f --ignore-unmatch " + pth,
                        shell=True,
                        capture_output=True,
                        check=True,
                    )
                    subprocess.run(
                        "rm -rf " + pth,
                        shell=True,
                        capture_output=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as e:
                    print(
                        "WARNING: error removing autogenerated "
                        "recipe for custom CDT %s: %s" % (cdt, repr(e))
                    )


def _clear_gen_cdts(pth):
    try:
        subprocess.run(
            "git rm -r -f --ignore-unmatch " + pth + "/*-*-*",
            shell=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            "rm -rf " + pth + "/*-*-*",
            shell=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print("WARNING: error removing autogenerated CDTs at %s: %s" % (pth, repr(e)))


def _fix_cdt_licenses(*, cdts, arch_dist_tuples, cdt_path):
    print("fixing CDT licenses for path '%s'..." % cdt_path, flush=True)
    for arch, dist in arch_dist_tuples:
        for cdt, cfg in cdts.items():
            pth = os.path.join(
                cdt_path,
                cdt.lower() + "-" + dist.replace("ent", "") + "-" + arch,
            )
            if 'license_file' in cfg and os.path.exists(pth):
                if cfg["license_file"] is None:
                    pass
                elif isinstance(cfg["license_file"], collections.abc.MutableSequence):
                    for lf in cfg['license_file']:
                        shutil.copy2(lf, os.path.join(pth, "."))
                else:
                    shutil.copy2(cfg['license_file'], os.path.join(pth, "."))

                try:
                    yaml = YAML(typ="jinja2")
                    yaml.indent(mapping=2, sequence=4, offset=2)
                    yaml.width = 320
                    with open(os.path.join(pth, "meta.yaml"), "r") as fp:
                        meta = yaml.load(fp)
                except Exception:
                    print("ERROR: could not adjust license for %s" % pth)
                    continue
                if cfg["license_file"] is None:
                    if "license_file" in meta["about"]:
                        meta["about"].pop("license_file")
                elif isinstance(cfg["license_file"], collections.abc.MutableSequence):
                    meta["about"]["license_file"] = [
                        os.path.basename(lf)
                        for lf in cfg["license_file"]
                    ]
                else:
                    meta["about"]["license_file"] = os.path.basename(
                        cfg["license_file"]
                    )

                with open(os.path.join(pth, "meta.yaml"), "w") as fp:
                    meta = yaml.dump(meta, fp)


def _fix_cdt_deps(*, cdts, arch_dist_tuples, cdt_path):
    print("adjusting CDT deps for path '%s'..." % cdt_path, flush=True)
    for arch, dist in arch_dist_tuples:
        for cdt, cfg in cdts.items():
            pth = os.path.join(
                cdt_path,
                cdt.lower() + "-" + dist.replace("ent", "") + "-" + arch,
            )
            if 'dep_remove' in cfg and os.path.exists(pth):
                try:
                    yaml = YAML(typ="jinja2")
                    yaml.indent(mapping=2, sequence=4, offset=2)
                    yaml.width = 320
                    with open(os.path.join(pth, "meta.yaml"), "r") as fp:
                        meta = yaml.load(fp)
                except Exception:
                    print("ERROR: could not adjust license for %s" % pth)
                    continue

                if "requirements" in meta:
                    for sec in ["build", "host", "run"]:
                        if sec in meta["requirements"]:
                            new_deps = []
                            for dep in meta["requirements"][sec]:
                                if not any(
                                    dep.startswith(d + "-cos")
                                    for d in cfg["dep_remove"]
                                ):
                                    new_deps.append(dep)
                            meta["requirements"][sec] = new_deps

                    with open(os.path.join(pth, "meta.yaml"), "w") as fp:
                        meta = yaml.dump(meta, fp)


def _fix_cdt_builds(*, cdts, arch_dist_tuples, cdt_path):
    print("adjusting CDT builds for path '%s'..." % cdt_path, flush=True)
    for arch, dist in arch_dist_tuples:
        shortdist = dist.replace("ent", "")
        distarch = dist.replace("ent", "") + "-" + arch
        for cdt, cfg in cdts.items():
            pth = os.path.join(
                cdt_path,
                cdt.lower() + "-" + distarch,
            )
            build_pth = os.path.join(pth, "build.sh")
            if (
                'build_append' in cfg
                and os.path.exists(pth)
                and (
                    distarch in cfg["build_append"]
                    or shortdist in cfg["build_append"]
                    or arch in cfg["build_append"]
                    or "all" in cfg["build_append"]
                )
            ):
                if distarch in cfg["build_append"]:
                    extra_build = cfg["build_append"][distarch]
                elif shortdist in cfg["build_append"]:
                    extra_build = cfg["build_append"][shortdist]
                elif arch in cfg["build_append"]:
                    extra_build = cfg["build_append"][arch]
                elif "all" in cfg["build_append"]:
                    extra_build = cfg["build_append"]["all"]
                else:
                    raise RuntimeError("could not get build append for %s!" % cdt)

                with open(build_pth, "r") as fp:
                    build_str = fp.read()

                if (
                    "# CONDA-FORGE BUILD APPEND" not in build_str
                    and not build_str.strip().endswith("# CDT BUILD APPENDED")
                ):
                    with open(build_pth, "w") as fp:
                        for line in build_str.splitlines():
                            fp.write(line + "\n")
                            if "# START OF INSERTED BUILD APPENDS" == line:
                                fp.write("\n\n# CONDA-FORGE BUILD APPEND\n")
                                fp.write(extra_build + "\n")


@click.command()
@click.option(
    "--force", default=False, is_flag=True,
    help="Forcibly regenerate all CDT recipes."
)
@click.option(
    "--no-legacy", default=False, is_flag=True,
    help="Do not generate the old-style, legacy CDTs in the legacy_* folders."
)
@click.option(
    "--fast", default=False, is_flag=True,
    help="Use a global src cache. May fail due to race conditions."
)
@click.option(
    "--keep-url-changes", default=False, is_flag=True,
    help="Keep changes to CDT urls. If you use this, you need to bump the build number!"
)
def _main(force, no_legacy, fast, keep_url_changes):
    """
    Generate all CDT recipes.
    """
    yaml = YAML()

    with open("cdt_slugs.yaml", "r") as fp:
        cdts = yaml.load(fp)

    if not no_legacy:
        os.makedirs(LEGACY_CDT_PATH, exist_ok=True)
        os.makedirs(LEGACY_CUSTOM_CDT_PATH, exist_ok=True)
    os.makedirs(CDT_PATH, exist_ok=True)
    os.makedirs(CUSTOM_CDT_PATH, exist_ok=True)

    print("generating CDT recipes...")
    futures = {}
    with ThreadPoolExecutor(max_workers=16) as exec:
        if not no_legacy:
            # legacy CDTs for the old compiler sysroots

            # if force:
            #     _clear_gen_cdts(LEGACY_CDT_PATH)

            extra = "--conda-forge-style"
            if fast:
                extra += " --use-global-cache"
            arch_dist_tuples = [
                ("x86_64", "centos6"),
                ("aarch64", "centos7"),
                ("ppc64le", "centos7")
            ]

            futures.update(
                _make_cdt_recipes(
                    extra=extra,
                    cdt_path=LEGACY_CDT_PATH,
                    arch_dist_tuples=arch_dist_tuples,
                    cdts=cdts,
                    exec=exec,
                    force=force)
                )

        # new CDTs for the new compilers with a single sysroot
        # if force:
        #     _clear_gen_cdts(CDT_PATH)

        extra = "--conda-forge-style --single-sysroot"
        if fast:
            extra += " --use-global-cache"
        arch_dist_tuples = [
            ("x86_64", "centos6"), ("x86_64", "centos7"),
            ("aarch64", "centos7"), ("ppc64le", "centos7")
        ]
        futures.update(
            _make_cdt_recipes(
                extra=extra,
                cdt_path=CDT_PATH,
                arch_dist_tuples=arch_dist_tuples,
                cdts=cdts,
                exec=exec,
                force=force)
            )

        for fut in tqdm.tqdm(as_completed(futures), total=len(futures)):
            c = fut.result()
            pkg = futures[fut]
            nm = "-".join([pkg["cdt"], pkg["dist"].replace("ent", ""), pkg["arch"]])
            if (
                c.returncode != 0
                or "WARNING: Did not find package called (or another one providing)" in c.stdout  # noqa
            ):
                tqdm.tqdm.write("WARNING: making CDT recipe %s failed!" % nm)
                tqdm.tqdm.write(c.stdout)

            if (
                "WARNING: could not find a suitable license " in c.stdout
            ):
                for line in c.stdout.splitlines():
                    if "WARNING: could not find a suitable license " in line:
                        _found_cdt = None
                        for _cdt in cdts:
                            if _cdt.lower() in line.lower():
                                if _found_cdt is None:
                                    _found_cdt = _cdt
                                elif len(_cdt) > len(_found_cdt):
                                    _found_cdt = _cdt

                        if _found_cdt is not None:
                            if "license_file" not in cdts[_found_cdt]:
                                tqdm.tqdm.write(line.strip())
                        else:
                            tqdm.tqdm.write(line.strip())

    # finally, we have to clean up any CDTs marked as custom that happened to be
    # made by the templates again
    if not no_legacy:
        # legacy CDTs for the old compiler sysroots
        arch_dist_tuples = [
            ("x86_64", "centos6"),
            ("aarch64", "centos7"),
            ("ppc64le", "centos7")
        ]
        _cleanup_custom_cdt_overlaps(
            cdt_path=LEGACY_CDT_PATH,
            arch_dist_tuples=arch_dist_tuples,
            cdts=cdts)

        _fix_cdt_licenses(
            cdts=cdts,
            arch_dist_tuples=arch_dist_tuples,
            cdt_path=LEGACY_CDT_PATH
        )

        _fix_cdt_deps(
            cdts=cdts,
            arch_dist_tuples=arch_dist_tuples,
            cdt_path=LEGACY_CDT_PATH
        )

        _fix_cdt_builds(
            cdts=cdts,
            arch_dist_tuples=arch_dist_tuples,
            cdt_path=LEGACY_CDT_PATH
        )

        if not keep_url_changes:
            _ignore_url_build_changes(
                LEGACY_CDT_PATH
            )

    # new CDTs for the new compilers with a single sysroot
    arch_dist_tuples = [
        ("x86_64", "centos6"), ("x86_64", "centos7"),
        ("aarch64", "centos7"), ("ppc64le", "centos7")
    ]
    _cleanup_custom_cdt_overlaps(
        cdt_path=CDT_PATH,
        arch_dist_tuples=arch_dist_tuples,
        cdts=cdts)

    _fix_cdt_licenses(
        cdts=cdts,
        arch_dist_tuples=arch_dist_tuples,
        cdt_path=CDT_PATH
    )

    _fix_cdt_deps(
        cdts=cdts,
        arch_dist_tuples=arch_dist_tuples,
        cdt_path=CDT_PATH
    )

    _fix_cdt_builds(
        cdts=cdts,
        arch_dist_tuples=arch_dist_tuples,
        cdt_path=CDT_PATH
    )

    if not keep_url_changes:
        _ignore_url_build_changes(
            CDT_PATH
        )

    # make the readme
    render_readme()

    print(
        "finished generating CDTs - make sure to add any changes in the repo "
        "via 'git add *' before making a commit!",
        flush=True,
    )


if __name__ == "__main__":
    _main()
