"""Build the identity dictionary from the corpus layout and map paths."""
from __future__ import annotations

import os
import re

from .identities import distinctive, pseudo, variants
from .replace import Replacer, apply_text
from .seeds import SEEDS

HEX12 = re.compile(r"^[0-9a-f]{7,40}$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9\-]+\.)+[a-z]{2,}$", re.I)

# Native CAD formats whose file names are used as links by assemblies and
# drawings. Their *names* are kept so links keep resolving; their folders are
# still renamed (CAD tools search the assembly's own folder).
NATIVE_LINKED = {
    "sldprt", "sldasm", "slddrw", "ipt", "iam", "idw", "ipn", "dwg",
    "par", "asm", "psm", "dft", "prt", "drw", "catpart", "catproduct",
    "catdrawing", "x_t", "x_b", "neu", "sprt", "jt",
}
CREO_VERSIONED = re.compile(r"\.(prt|asm|drw|frm|neu|sec|lay)\.\d+$", re.I)

COMMON_WORDS = set(
    """
    cad cam files file drawings drawing models model 3d 2d design designs mechanical
    mechanics projects project parts part hardware library libraries practice
    engineering advanced exercises modeling modelling solidworks freecad public
    main final assembly assemblies portfolio portfoilo resources tutorials labs
    lab practices with for of the and a in on to me cad-files docs documentation
    machine machines robot robotic arm 4dof printed gearbox design engine radial
    examples models3d website source open hand mechanicaldesign 3dmodels
    """.split()
)

WEB_GENERIC_LABELS = {
    "www", "cdn", "static", "media", "files", "assets", "blob", "core", "windows",
    "net", "com", "org", "edu", "prod", "cloud", "dxcloud", "download", "wiki",
    "ftp", "data", "nvlpubs", "dropbox", "shopify", "home", "staff", "peer",
    "contenthub", "content", "dc", "ed", "us", "dcc", "upskill", "dev", "github",
    "io", "co", "ac", "uk", "de", "fr", "cn", "jp", "at", "eu", "info",
}


def _is_generic_repo(name: str) -> bool:
    words = [w for w in re.split(r"[\s_\-\.]+", name.lower()) if w]
    return bool(words) and all(w in COMMON_WORDS or w.isdigit() for w in words)


def split_owner_repo(comp: str):
    """'owner__repo__abcdef123456' -> (owner, repo, sha_suffix or '')."""
    parts = comp.split("__")
    if len(parts) >= 3 and HEX12.match(parts[-1]):
        return parts[0], "__".join(parts[1:-1]), parts[-1]
    if len(parts) >= 2:
        return parts[0], "__".join(parts[1:]), ""
    return None


class Layout:
    def __init__(self, rel_paths: list[str], discovered: list | None = None):
        self.entries: list[tuple[str, str, str]] = []  # (spelling, kind, ident)
        self.repos: dict[str, tuple[str, str]] = {}  # top component -> (owner, repo)
        seen = set()

        def add(spelling, kind, ident):
            k = (spelling.lower(), kind)
            if k in seen:
                return
            seen.add(k)
            self.entries.append((spelling, kind, ident))

        # 1. hand-reviewed seeds first (most specific)
        for kind, name, extra in SEEDS:
            for sp in variants(name):
                add(sp, kind, name)
            for e in extra:
                for sp in variants(e):
                    add(sp, kind, name)

        # 2. identities discovered inside the corpus (registry from the scan)
        for sp, kind, ident in discovered or []:
            add(sp, kind, ident)

        tops_gh, tops_gl, domains = set(), set(), set()
        for p in rel_paths:
            c = p.split("/")
            if c[0] == "archives" and len(c) >= 2:
                tops_gh.add(os.path.splitext(c[1])[0])
            elif len(c) >= 3 and c[0] == "sources":
                if c[1] == "github":
                    tops_gh.add(c[2])
                elif c[1] == "gitlab":
                    tops_gl.add(c[2])
                elif c[1] in ("web",):
                    domains.add(c[2])
                elif c[1] == "catalog" and DOMAIN_RE.match(c[2]):
                    domains.add(c[2])

        def add_owner_repo(owner, repo):
            if owner:
                for sp in variants(owner):
                    if distinctive(sp):
                        add(sp, "owner", owner)
            if repo and not _is_generic_repo(repo):
                for sp in variants(repo):
                    if distinctive(sp) and not _is_generic_repo(sp):
                        add(sp, "project", repo)
                # product names that lead a repo name: PAROL6-Desktop-robot-arm -> PAROL6
                head = re.split(r"[\s_\-\.]+", repo)[0]
                if head != repo and len(head) >= 4 and distinctive(head) and head.lower() not in COMMON_WORDS \
                        and (re.search(r"\d", head) or re.search(r"[A-Z]", head[1:]) or head.isupper()):
                    for sp in variants(head):
                        add(sp, "project", repo)
                    core = re.sub(r"\d+$", "", head)
                    if core != head and len(core) >= 5 and core.lower() not in COMMON_WORDS and distinctive(core):
                        add(core, "project", repo)

        for t in sorted(tops_gh):
            r = split_owner_repo(t)
            if r:
                owner, repo, _ = r
                self.repos[t] = (owner, repo)
                add_owner_repo(owner, repo)
        for t in sorted(tops_gl):
            parts = [x for x in t.split("__")]
            host, rest = parts[0], [x for x in parts[1:] if x]
            if rest:
                owner, repo = rest[0], rest[-1]
                self.repos[t] = (owner, repo)
                add_owner_repo(owner, repo)
                for mid in rest[1:-1]:
                    if distinctive(mid) and not _is_generic_repo(mid):
                        add(mid, "project", mid)
        for d in sorted(domains):
            dd = d.lower()
            base = dd[4:] if dd.startswith("www.") else dd
            add(dd, "domain", base)
            add(base, "domain", base)
            labels = base.split(".")
            # the registrable label, e.g. dcc.ligo.org -> ligo
            for lab in labels[:-1]:
                if lab not in WEB_GENERIC_LABELS and distinctive(lab):
                    for sp in variants(lab):
                        if distinctive(sp):
                            add(sp, "org", lab)
        self.domains = domains
        self.rep = Replacer(self.entries)

    # -- path mapping --------------------------------------------------------
    def _map_owner_repo_comp(self, comp: str) -> str:
        r = split_owner_repo(comp)
        if not r:
            return apply_text(comp, self.rep)
        owner, repo, sha = r
        out = [pseudo("owner", owner), pseudo("project", repo)]
        if sha:
            out.append(sha)
        return "__".join(out)

    def _map_gitlab_comp(self, comp: str) -> str:
        parts = comp.split("__")
        host, rest = parts[0], parts[1:]
        nonempty = [i for i, x in enumerate(rest) if x]
        out = []
        for i, x in enumerate(rest):
            if not x:
                out.append(x)
            elif i == nonempty[0]:
                out.append(pseudo("owner", x))
            else:
                out.append(pseudo("project", x))
        return "__".join([host] + out)

    def _map_name(self, comp: str, is_file: bool, repo_ctx) -> str:
        # repo-prefixed folders inside extracted archives: '<repo>.extracted',
        # '<repo>-<sha>', '<repo>-master'
        if repo_ctx:
            owner, repo = repo_ctx
            low, rl = comp.lower(), repo.lower()
            if low.startswith(rl) and len(comp) > len(repo) and comp[len(repo)] in ".-_":
                tail = comp[len(repo):]
                if tail in (".extracted",) or re.match(r"^[-_]([0-9a-f]{7,40}|master|main)(\.extracted)?$", tail, re.I) \
                        or (is_file and re.match(r"^\.(zip|tar|tgz|7z|tar\.gz)$", tail, re.I)):
                    return pseudo("project", repo) + tail
        if is_file:
            if CREO_VERSIONED.search(comp):
                return comp
            stem, ext = os.path.splitext(comp)
            if ext[1:].lower() in NATIVE_LINKED:
                return comp
            if comp.endswith(".resume-partial"):
                inner, _ = os.path.splitext(stem)
                return self._map_name(stem, True, repo_ctx) + ".resume-partial"
            return apply_text(stem, self.rep) + ext
        if comp.endswith(".extracted"):
            return self._map_name(comp[: -len(".extracted")], True, repo_ctx) + ".extracted"
        return apply_text(comp, self.rep)

    def map_path(self, rel: str) -> str:
        c = rel.split("/")
        out = list(c)
        start = 0
        repo_ctx = None
        if c[0] == "archives" and len(c) >= 2:
            stem, ext = os.path.splitext(c[1])
            repo_ctx = self.repos.get(stem)
            out[1] = self._map_owner_repo_comp(stem) + ext if repo_ctx else self._map_name(c[1], len(c) == 2, None)
            start = 2
        elif len(c) >= 3 and c[0] == "sources":
            kind = c[1]
            if kind == "github":
                repo_ctx = self.repos.get(c[2])
                out[2] = self._map_owner_repo_comp(c[2])
            elif kind == "gitlab":
                repo_ctx = self.repos.get(c[2])
                out[2] = self._map_gitlab_comp(c[2])
            elif kind == "web" or (kind == "catalog" and DOMAIN_RE.match(c[2])):
                d = c[2].lower()
                out[2] = pseudo("domain", d[4:] if d.startswith("www.") else d)
            else:
                out[2] = self._map_name(c[2], len(c) == 3, None)
            start = 3
        for i in range(start, len(c)):
            out[i] = self._map_name(c[i], i == len(c) - 1, repo_ctx)
        return "/".join(out)

    def map_member(self, member: str, repo_ctx) -> str:
        """Map a path inside an archive (no corpus prefix)."""
        c = member.split("/")
        return "/".join(
            self._map_name(x, i == len(c) - 1, repo_ctx) if x else x for i, x in enumerate(c)
        )
