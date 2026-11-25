"""No tracked file names a customer, an employer, a person or a private system.

The list holds sha256 hashes of lowercased tokens, so reading it does not show what it forbids. Unsalted,
so a guess can still be checked: it stops accidental disclosure, not a determined reader. To add a name: `printf %s 'name' | shasum -a 256`, lowercased. A word is a maximal run of letters and
digits, so a name inside a longer word never matches; a listed token with `-` or `.` in it matches
those exact adjacent words. A weekday or other common word can only be listed in such a compound.
"""
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_COMPOUND = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_WORD = re.compile(r"[a-z0-9]+")
_MAX_WORDS = 3                              # the longest listed token is three words

PRIVATE = {
    "095c2bb2889cab1c6cc3ae8b65e2987d2ad86c7cde35881953bee25dd57f6de1",
    "09850d92cdb8fae15b71dce59c869dcdacb0dbfb27afdae5c729ff3503e7bd25",
    "0f367f46dc4cea7aa9659e4df557cf0d10284ce7306ae4d8bec79c18bc19396e",
    "13f1a6081cae256fe5571798704fc6892832b7d423d4f63df4a87d57a834fe0a",
    "19fdae560ec445e89bf1379cd4b4d06e6024b8e12bb7ddbbf293f892596bbae5",
    "2587ecc82db5f9a931ef8e2316b20f103f107225fa267ec469fcf1c86a677baf",
    "308c41fa2f1e329e8213880c0b5d5cee16e659e05c6be117ae40dc021291d53e",
    "36c3a78408c6f1c325b1b9b08177525f64c88d01068a1926d64a8c89f8c90927",
    "458bf7845b2b9d029428a4afabfb1e691b21faa0ef75d1f8e59e1f025f978458",
    "4c7a4ee17f3d12c2b4f06a432fc59178e2d66308696645703c0d0ecd56074bad",
    "4e1606d42be268a42a6a88d44814b8474135e6b2ebff654495b5158903b87210",
    "51b0749346ebeeed821e310457f1b31b4f87b82593e720861f8699441aad90ac",
    "52c899ebc0453edac7090404703dc51b57102204faa7d20fdc64154347800e60",
    "5445efa663d0a55066f20d4cf831af733ccfb50693700b732df31becbb758708",
    "5698afafd3dda712b550a5869d8bc1f75276ed017669b4f7bde46b5c87baeeb7",
    "58c7e8fec111db5bef05708d1f0b44191e9a34d198febb3c7ba7755260d73e2b",
    "6855c9179e07b4c9cb0db952fc724bb35ec9544a9cd6a9d754e466eee7cd09dc",
    "6b5e57d3b6eb4ed44eda2ec43eb4edda23035084d4de313db55f45bdbad96326",
    "743afb03ddee4b9ab9accb90896c4eb4af62bfbe815f2322c5a32e53773fe284",
    "771795917a4700741d27b968cf422b0985ba45e1d45937413135f579cb6aff02",
    "7768b92c286a9419d81ae160b4778a435992908a518c95669521887af721f893",
    "78f0d3a83755a5319726709e256865efb3cd195f1dfbcf0f14c515de3253a1db",
    "84edd85363cd5edb6b506cd4f7b6b596297193717f04b7d726ddcac3d236180a",
    "858cd2738beae5ee00e8179f86f4b59c55eb7e24e83b6302173eff342f241944",
    "954adfa4d922fa301b7cb094dcdfaa4dd12fdf957e93b59a2fedc323b418c004",
    "97da0d57c8f8e9025d5c55b7d484e50aaccd2d284b8ff8afddab45627cb58331",
    "99cb0d293f888387349a76e7e347122244b4766f28ec11765b9569d06f48a983",
    "a3057cc51bb1c2b588dcc5e28fb3f36e1ee93bd7c4b26ff2fb0111084e1c831d",
    "a336a46f53da6ce990377cb18977bd2e31f94f725f0282b1026058e5c39ad863",
    "a345eea0d7416a498319c5a303b9fbdd9138e7e708b3f393fc1f0fdf58cb1872",
    "a6053ef7f9555e3be083b5e60350e69330f90759fc522a6b8b0246d4b04da717",
    "a9ab5d7a528d15d6660e6884e7201b494f90d5ea3d97eee6d75688c081bfc9b6",
    "aa48cda33830e78cf2e7da0d354f0fc7ab765970039dce0718eaa576be6cd261",
    "aae8a82ae19ed7a2a179941c7ec4100ae9e95bc86256da59cd288bd7e0d1731a",
    "af7b08d2b06b8c1a6b1b2961579885bb3185703946dd169ac51c1d00b4a59481",
    "b28df9f73943847436280b13e7cf1563676a2a1e1ef04aa5f43bbf7d1de9ea1b",
    "b28f7d7d01283d2dac955641e24dffb3daf063622cf73c73a1f83c336c27033b",
    "b3f4e629d80c7246fefdaf43c96424deca91467d9a2612133de520e940c9f30a",
    "b47015f2c508930e046c8873b0fb29c6a4a8f3fb365155dc353afaa21ece5829",
    "b47ca91c252183a0150174aa8766f34038399b597efca30fc11f75ac9c93f4bf",
    "b73a748f69985b9f21bd1b20b5a3a8f7660e67d890bcb742e905b7bf6911afd7",
    "be66d9594499b9c55db1e1d3eae1e68e165ef6bcff41cab3f7e1830d7a5b8ff9",
    "bfc3404ffe01c37c3870d0f5b0b3b1e84ff6b2fda96fb8a56ce1a96baa816565",
    "c335ea533095b32d02555e58197d2a46dc2f53b22187bd557d82685c50cf88dc",
    "c3bdf156402d6a2dfc87a27301428bc9aa4c2a3eed390d3dec5671fd4ba1a186",
    "c569038e49474b8601493b116e77ad54de36ac7b2ede58e05e5c43126a5bad9b",
    "c6b5e1ee4f27b8b299b740f70d2b16ae4866e6bd78b4400e02d4b9b630ae355e",
    "c733290426e4ff828d07e7a6f1efcaaa4fc035786e254649f06c6c66961fa7cd",
    "cb5a2a274b33fb64c76ddde4676d302755fefe023219936ce63c08a875b50d6e",
    "cce628dc11f283f9f89caee1d1512f50d5ca6e6bff7780c1d699cc793cde655f",
    "d0ac22b098411b1139cba62d60d4c825c6e3af415f2cc90df6ea4536d4772b89",
    "d1516b2a289ceb648e32ff1cf29fd1d6e8be283209cf2142d52ef76469fd3e0f",
    "d426a112996c09e7f48a796d7a0e0d00efc0723cb8cf9fee981640ea365c3c55",
    "d6286c5cedb888ebf992a92e94634b90aab5ad69e8765f65848a1fe947cf323f",
    "d7a66b55769d3dad133ea1004b0de98c60d0c33bb6189cfbd04d1aa00ba7ae9e",
    "d91381ad0f8d7721410b702782a1f59de423d0b78bb9c0e543c3400e25b742b0",
    "d9e2f2a872e6b436e074a87b0530da0fcf81744cc75aed5806e8b99f668c06cb",
    "dcb3f9f57f96d0204f87e23994cd89882d2c0312806e64e41c322d0bac3b9219",
    "dd564c78fa63f0844092a76d07fc806c94de2f86fd7d1fb65a8aafbf4d29c95a",
    "df9ba8c57b8c27950e198c6adda0f1874ba23a038e45b8a0c0a7c66e63e1e992",
    "e995b74b187b141b41190232c67b9647131bd54c7f5de2da5e017503c1ec50f5",
    "f420074d5d751e8f4c0fb5ad6d07d79542fee8829e0521a861bed8bc8086824d",
    "fbf4e1dc20043716a9abe69fe7a5af3614a8b2e88f9160267411ef826d8c3a17",
}

# Functional literals that production or an installed client still reads at runtime. Each is one
# path and one hash, so the guard stays strict everywhere else; drop the entry when the literal goes.
ALLOWED = set()


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens(text):
    """Every word of the text, plus every run of up to _MAX_WORDS adjacent words joined by - or ."""
    out = set()
    for compound in _COMPOUND.finditer(text.lower()):
        c = compound.group(0)
        spans = [m.span() for m in _WORD.finditer(c)]
        for i, (start, _) in enumerate(spans):
            for _, end in spans[i:i + _MAX_WORDS]:
                out.add(c[start:end])
    return out


def _leaks(text, forbidden):
    return {_sha(t) for t in _tokens(text)} & forbidden


def _tracked():
    return subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True,
                          check=True).stdout.split("\0")


def test_the_guard_catches_a_planted_name():
    zyxwv = {_sha("zyxwv")}
    assert _leaks("reach Zyxwv@Example.com today", zyxwv) == zyxwv
    assert _leaks("see #production-zyxwv (C0ABC)", zyxwv) == zyxwv
    assert _leaks("abczyxwvdef and zyxwv2", zyxwv) == set()
    assert _leaks("day zyx-wv.test", {_sha("zyx-wv")}) == {_sha("zyx-wv")}
    assert _leaks("day zyx.wv and zyx wv", {_sha("zyx-wv")}) == set()


def test_no_tracked_file_or_path_names_a_private_party():
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    leaks = []
    for path in filter(None, _tracked()):
        file = REPO / path
        found = _leaks(path, PRIVATE)
        if file.is_file():
            try:
                found |= _leaks(file.read_text(encoding="utf-8"), PRIVATE)
            except UnicodeDecodeError:
                pass
        leaks += [(path, h[:12]) for h in found if (path, h) not in ALLOWED]
    assert not leaks, f"private names in tracked files (path, hash prefix): {sorted(leaks)}"


def test_every_allowance_is_still_needed():
    stale = [(path, h[:12]) for path, h in ALLOWED
             if h not in _leaks((REPO / path).read_text(encoding="utf-8"), PRIVATE)]
    assert not stale, f"drop these from ALLOWED, the literal is gone: {stale}"
