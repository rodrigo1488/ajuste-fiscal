"""Escolhe a regra vigente da operação e completa CBS, IBS e Imposto Seletivo."""

from datetime import date

from ajuste_fiscal.banco import _json_numero


def carregar_regras(conexao, uf: str, regime: str, dia: date) -> list[dict]:
    return conexao.execute(
        """
        SELECT * FROM regras_fiscais
        WHERE (uf = '' OR uf = %s)
          AND (regime = '' OR regime = %s)
          AND vigencia_ini <= %s
          AND (vigencia_fim IS NULL OR vigencia_fim >= %s)
        """,
        (uf, regime, dia, dia),
    ).fetchall()


def escolher(regras: list[dict], tipo: str, ncm: str | None, cest: str | None, regime: str):
    melhor = None
    melhor_nota = None
    for regra in regras:
        if regra["tipo"] != tipo:
            continue
        nota = _nota(regra, ncm, cest, regime)
        if nota is None:
            continue
        if melhor_nota is None or nota > melhor_nota:
            melhor = regra
            melhor_nota = nota
    return melhor


def montar_tributos(produto: dict, regras: list[dict], oficiais: dict, regime: str, fabrica: bool) -> dict | None:
    ncm = produto.get("ncm")
    cest = produto.get("cest") or ""
    icms = escolher(regras, "icms", ncm, cest, regime)
    pis = escolher(regras, "pis_cofins", ncm, cest, regime)
    ipi = None if fabrica else escolher(regras, "ipi", ncm, cest, regime)
    classe = (oficiais.get("classes") or {}).get(produto.get("cclass_trib"))
    aliquotas = oficiais.get("aliquotas") or {}
    if icms is None or pis is None or ipi is None or classe is None:
        return None
    if aliquotas.get("CBS") is None or aliquotas.get("IBS_UF") is None or aliquotas.get("IBS_MUN") is None:
        return None
    vigencia = classe["vigencia"].isoformat() if classe.get("vigencia") else None
    amparo = classe.get("texto_curto") or classe.get("referencia_normativa") or ""
    grupo = {
        "cbs": {
            "cClassTrib": produto.get("cclass_trib"),
            "cst": classe.get("cst") or produto.get("cst_ibs_cbs"),
            "aliquota": _json_numero(aliquotas["CBS"]),
            "reducao": _json_numero(classe.get("reducao_cbs")) or 0,
            "amparoLegal": amparo,
            "vigencia": vigencia,
        },
        "ibs": {
            "cClassTrib": produto.get("cclass_trib"),
            "cst": classe.get("cst") or produto.get("cst_ibs_cbs"),
            "aliquotaUf": _json_numero(aliquotas["IBS_UF"]),
            "aliquotaMun": _json_numero(aliquotas["IBS_MUN"]),
            "reducaoUf": _json_numero(classe.get("reducao_ibs_uf")) or 0,
            "reducaoMun": _json_numero(classe.get("reducao_ibs_mun")) or 0,
            "amparoLegal": amparo,
            "vigencia": vigencia,
        },
        "pisCofins": {
            "cstSai": pis["cst"],
            "aliqPis": _json_numero(pis["aliq_pis"]),
            "aliqCofins": _json_numero(pis["aliq_cofins"]),
            "amparoLegal": pis["amparo_legal"],
        },
        "ipi": {
            "cstSai": ipi["cst"],
            "aliquota": _json_numero(ipi["aliq_ipi"]),
            "amparoLegal": ipi["amparo_legal"],
        },
        "regras": [_regra_icms(icms, regime)],
        "vigencia": vigencia,
    }
    seletivo = (oficiais.get("seletivos") or {}).get(ncm)
    if seletivo is not None and seletivo.get("tributado"):
        grupo["is"] = {
            "tributado": True,
            "aliquota": _json_numero(seletivo.get("aliquota_ad_valorem")),
            "amparoLegal": amparo,
        }
    return grupo


def _regra_icms(regra: dict, regime: str) -> dict:
    corpo = {
        "uf": regra["uf"],
        "cfop": regra["cfop"],
        "aliqIcms": _json_numero(regra["aliq_icms"]),
        "st": bool(regra["exige_cest"]),
        "amparoLegal": regra["amparo_legal"],
    }
    if regime == "SN":
        corpo["csosn"] = regra["csosn"]
    else:
        corpo["cst"] = regra["cst"]
    if regra.get("mva") is not None:
        corpo["mva"] = _json_numero(regra["mva"])
    return corpo


def _nota(regra: dict, ncm: str | None, cest: str | None, regime: str):
    if regra["cest"]:
        if regra["cest"] != (cest or ""):
            return None
        especificidade = 300
    elif regra["ncm"]:
        if not ncm or not (ncm == regra["ncm"] or (len(regra["ncm"]) < 8 and ncm.startswith(regra["ncm"]))):
            return None
        especificidade = 200 if ncm == regra["ncm"] else 100 + len(regra["ncm"])
    elif regra["exige_cest"]:
        if not cest:
            return None
        especificidade = 50
    else:
        especificidade = 0
    return (especificidade, 1 if regra["regime"] == regime else 0, regra["vigencia_ini"])
