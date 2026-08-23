# -*- coding: utf-8 -*-
"""F1 nonempty-hmtx multiset → F2 glyf length (Jasper co-generation).

Built from чеки/т банк OpenPDF genuines (n=128). Unknown F1 hmtx kits do not
fire (future genuines). Known kit with foreign Medium glyf = SEQ F2 transplant
while Regular metrics stayed on a twin kit.
"""

from __future__ import annotations

_F1_HMTX_TO_F2_GLYF: dict[str, frozenset[int]] = {
    "074bd70b546f2ea4": frozenset({1094}),
    "07d27b932d2ad532": frozenset({1212}),
    "092f60f17894ca34": frozenset({992}),
    "0d5332bc64e3095a": frozenset({1260}),
    "0e93796b844c375c": frozenset({1094}),
    "1137ea41d1d769cd": frozenset({1096}),
    "135d595bfd4fb98a": frozenset({812}),
    "161e29288263563b": frozenset({1530}),
    "16b99cef58b795e0": frozenset({822}),
    "1c8cc0bac17cbb16": frozenset({1260}),
    "1cda3349e3dc4a7b": frozenset({1260}),
    "1e47af05f2a20f06": frozenset({1320}),
    "20ffc0c8b1c2caa1": frozenset({1322}),
    "21d0e47c93e515d7": frozenset({1094}),
    "23b7d7eb0a3c8839": frozenset({856}),
    "2469ac8cc4648dab": frozenset({856}),
    "2581b09c7807efa2": frozenset({1144}),
    "2b92f93d63efd278": frozenset({866}),
    "2c35dee121580939": frozenset({954}),
    "3294d8918474ab87": frozenset({1096}),
    "349fe16b62a356bd": frozenset({1228}),
    "358d55ebb5adaaea": frozenset({1226}),
    "35a84e02a88c8662": frozenset({992}),
    "35f99b40c5d51967": frozenset({1096}),
    "36f2270dbda76708": frozenset({1012}),
    "3a9bfa09b441d6f7": frozenset({1554}),
    "3b80d77a3295ae8d": frozenset({1008}),
    "3c9545d92e7aa0a7": frozenset({1282}),
    "3d734755190975ca": frozenset({1008}),
    "3ec4f482fd411a06": frozenset({1232}),
    "404d6e568e301495": frozenset({954}),
    "4069abfc784f66c4": frozenset({974}),
    "41fbb3b42022a288": frozenset({1226}),
    "4330189a947a34fa": frozenset({1226}),
    "45bd1747b88861a9": frozenset({954}),
    "4b0d164c15331f25": frozenset({1106}),
    "4b3745db0c9b9a26": frozenset({1454}),
    "4b5d2057c595c92c": frozenset({866}),
    "4c61d34389745651": frozenset({1284}),
    "4ea9d6aa16846e9c": frozenset({1144}),
    "504d448044100e35": frozenset({1226}),
    "516fb93de95e980d": frozenset({1322}),
    "52e3d82d9e1f3d46": frozenset({1322}),
    "53045aff29a9f7ba": frozenset({1228}),
    "556e4c1b31d8867b": frozenset({1094}),
    "57b09df124a5716e": frozenset({1232}),
    "5854ef2000a087dc": frozenset({1046}),
    "5c2d4754d6dc9263": frozenset({1232}),
    "5f593f6a36c961a7": frozenset({934, 1134}),
    "60192acf2c183891": frozenset({1094}),
    "607d19f7b3119fa7": frozenset({1636}),
    "6110e18d737755a8": frozenset({1232}),
    "61ae0b883c67b159": frozenset({1248}),
    "6222029ef7ad850a": frozenset({1062}),
    "6e318515ba692e5e": frozenset({866}),
    "6e4299b78946bd13": frozenset({1096}),
    "6f057ab380e48202": frozenset({1008, 1096}),
    "70f163ab0b6fdb02": frozenset({1008}),
    "76d62cdc83df276e": frozenset({1214}),
    "77471dd9f023d013": frozenset({1472}),
    "7790604fcc01354d": frozenset({856}),
    "82f59a4708aa8064": frozenset({1106}),
    "86f1d7622b7e8f15": frozenset({992}),
    "8743d7965a9dcbbb": frozenset({992}),
    "877747f4547de0a3": frozenset({1246}),
    "87a7da3c57282c98": frozenset({1100}),
    "89cf49cd36976c05": frozenset({1144}),
    "8ea5a4f526dce7b7": frozenset({1094}),
    "8efadd40e2f3bc99": frozenset({1374}),
    "90f4e68ef30748c9": frozenset({1144}),
    "92a2664d3c4ad075": frozenset({1264}),
    "97bcc8664506364b": frozenset({974}),
    "984ddae97a24ce75": frozenset({988, 1062}),
    "98c46a80abd97fcc": frozenset({1226}),
    "9a1ee298e94b5d0f": frozenset({856}),
    "9c042fdfdd4d36ce": frozenset({1322}),
    "9ceb4c530b60814a": frozenset({1226}),
    "9f9dd5000f87273b": frozenset({1248}),
    "ab1a6b27a6ec6366": frozenset({992}),
    "ab70518a0d266518": frozenset({1096}),
    "b061f3975e92b9f5": frozenset({1322}),
    "b2b396d135223e47": frozenset({1106}),
    "b372fbbb3a4f8f1b": frozenset({1232}),
    "b7787bc700723102": frozenset({904}),
    "b78399e9c3886703": frozenset({1680}),
    "ba87927786e40104": frozenset({992}),
    "bf023a4e7647f39e": frozenset({1214}),
    "c48a2fcfd7671eac": frozenset({1144}),
    "c49e671e50ad44ec": frozenset({992}),
    "c6999dfa08badd42": frozenset({1932}),
    "cb063b00c9b7ca81": frozenset({1144}),
    "cbc8a8e2f7795fe3": frozenset({992}),
    "d23ec7eec6545908": frozenset({866}),
    "d569aa935113d410": frozenset({904}),
    "d6ec317243394b42": frozenset({992}),
    "d7402badffdc9646": frozenset({1242}),
    "d9483c17b1e8df49": frozenset({904}),
    "ddf42bc0f0293629": frozenset({992}),
    "e026f4f95299e325": frozenset({1228}),
    "e053e324ed8d94b2": frozenset({1720}),
    "e0f9edff6b17892d": frozenset({992}),
    "e15d852aa53a83a2": frozenset({1226}),
    "e27d8b8a1392b1d6": frozenset({1506}),
    "e3de06acfcb7c310": frozenset({1226}),
    "e3e4973fffa012ca": frozenset({1012}),
    "e61c5ad5c9f3d79a": frozenset({954}),
    "ea3e6e32cfca94e7": frozenset({1226}),
    "f03652f8b6099db6": frozenset({954}),
    "f121c819ed0c40f5": frozenset({1226}),
    "f5dc9bc2ada181e8": frozenset({1096}),
    "fcba002fb8c3bd3f": frozenset({1144}),
}

# F1.glyf lengths that arm SIZE_MULTISET / SHAPE_ENVELOPE (Jasper genuines).
F1_TWIN_GLYF_LENS = frozenset({
    12080, 12170, 12176, 12178, 12236, 12308, 12328, 12344, 12398, 12422,
    12434, 12442, 12468, 12482, 12520, 12528, 12530, 12554, 12560, 12592,
    12596, 12598, 12612, 12626, 12684, 12700, 12720, 12768, 12784, 12816,
    12818, 12852, 12864, 12910, 12978, 13000, 13002, 13038, 13210, 13264,
    13610, 13794, 14032,
})


def f1_nonempty_hmtx_sha16(ttf: bytes) -> str | None:
    """Anonymous multiset of hmtx advances for nonempty glyphs (sha16)."""
    if not ttf:
        return None
    try:
        import hashlib
        from io import BytesIO
        from fontTools.ttLib import TTFont

        tt = TTFont(BytesIO(ttf))
        if "loca" not in tt or "hmtx" not in tt:
            tt.close()
            return None
        loca = list(tt["loca"])
        advs = sorted(
            int(tt["hmtx"].metrics[name][0])
            for i, name in enumerate(tt.getGlyphOrder())
            if int(loca[i + 1]) > int(loca[i])
        )
        tt.close()
        return hashlib.sha256(",".join(map(str, advs)).encode()).hexdigest()[:16]
    except Exception:
        return None


def allowed_f2_glyf_for_f1(ff1: bytes) -> frozenset[int] | None:
    sha = f1_nonempty_hmtx_sha16(ff1)
    if not sha:
        return None
    return _F1_HMTX_TO_F2_GLYF.get(sha)
