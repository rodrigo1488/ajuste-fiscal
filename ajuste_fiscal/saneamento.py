"""Consulta em lote no formato de saneamento para outros ERPs."""

from datetime import date

from ajuste_fiscal.banco import (
    conectar,
    garantir_schema,
    parceiro_por_token,
    salvar_sugestao,
    url_banco,
)
from ajuste_fiscal.normalizar import codigo_ncm, normalizar_ean, normalizar_texto
from ajuste_fiscal.regras import carregar_regras, montar_tributos

LIMITE = 100
REGIMES = {"SN", "LP", "LR"}


class SemToken(PermissionError):
    """O pedido chegou sem um parceiro ativo."""


def sanear(corpo: dict, token: str, url: str | None = None, dia: date | None = None) -> dict:
    if parceiro_por_token(token, url) is None:
        raise SemToken("Token ausente ou inválido.")
    emit, perfil, produtos = _validar(corpo)
    hoje = dia or date.today()
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        regras = carregar_regras(conexao, perfil["uf"], emit["regime"], hoje)
        oficiais = _oficiais(conexao)
        encontrados = _buscar(conexao, produtos)
        grupos = []
        ausentes = []
        for item in produtos:
            resultado = _um(conexao, item, encontrados, regras, oficiais, emit["regime"], perfil["fabrica"])
            if resultado["grupo"] is None:
                ausentes.append(resultado["ausente"])
            else:
                grupos.append(resultado["grupo"])
        conexao.commit()
    return {
        "cabecalho": {
            "prodEnv": len(produtos),
            "prodRet": len(grupos),
            "prodNaoRet": len(ausentes),
            "mensagem": "OK",
        },
        "grupos": grupos,
        "naoRetornados": ausentes,
    }


def _validar(corpo: dict):
    if not isinstance(corpo, dict):
        raise ValueError("Informe o emitente, o perfil e os produtos.")
    emit = corpo.get("emit") if isinstance(corpo.get("emit"), dict) else None
    perfil = corpo.get("perfil") if isinstance(corpo.get("perfil"), dict) else None
    produtos = corpo.get("produtos")
    if emit is None or perfil is None or not isinstance(produtos, list) or not produtos:
        raise ValueError("Informe o emitente, o perfil e os produtos.")
    if len(produtos) > LIMITE:
        raise ValueError(f"O lote aceita até {LIMITE} produtos.")
    regime = str(emit.get("regime") or emit.get("regimeTrib") or "").strip().upper()
    if regime not in REGIMES:
        raise ValueError("Regime desconhecido. Use SN, LP ou LR.")
    uf_emit = str(emit.get("uf") or "").strip().upper()
    uf = str(perfil.get("uf") or "").strip().upper()
    if isinstance(perfil.get("uf"), list):
        uf = str(perfil["uf"][0] or "").strip().upper() if perfil["uf"] else ""
    if uf != "SP" or (uf_emit and uf_emit != "SP"):
        raise ValueError("O primeiro recorte atende só operações internas de São Paulo.")
    cfop = "".join(caractere for caractere in str(perfil.get("cfop") or "") if caractere.isdigit())
    if cfop and not cfop.startswith("5"):
        raise ValueError("O primeiro recorte atende só a saída interna.")
    return (
        {"regime": regime, "uf": uf_emit or "SP"},
        {"uf": uf, "fabrica": bool(perfil.get("fabricacaoPropria"))},
        produtos,
    )


def _oficiais(conexao) -> dict:
    return {
        "classes": {linha["codigo"]: linha for linha in conexao.execute("SELECT * FROM cclass_oficial").fetchall()},
        "aliquotas": {
            linha["tributo"]: linha["aliquota"]
            for linha in conexao.execute("SELECT tributo, aliquota FROM aliquota_oficial").fetchall()
        },
        "seletivos": {linha["ncm"]: linha for linha in conexao.execute("SELECT * FROM seletivo_oficial").fetchall()},
    }


def _buscar(conexao, produtos: list[dict]) -> dict:
    eans = []
    normas = []
    for item in produtos:
        if not isinstance(item, dict):
            continue
        if _interno(item):
            texto = str(item.get("descricao") or "").strip()
            if texto:
                normas.append(normalizar_texto(texto))
            continue
        codigo = normalizar_ean(item.get("codigo"))
        if codigo:
            eans.append(codigo)
        texto = str(item.get("descricao") or "").strip()
        if texto:
            normas.append(normalizar_texto(texto))
    por_ean = {
        linha["ean"]: linha
        for linha in (
            conexao.execute("SELECT * FROM produtos WHERE ean = ANY(%s)", (eans,)).fetchall() if eans else []
        )
    }
    por_descricao = {
        linha["descricao_norm"]: linha
        for linha in (
            conexao.execute("SELECT * FROM produtos WHERE descricao_norm = ANY(%s)", (normas,)).fetchall()
            if normas
            else []
        )
    }
    ids = list({linha["id"] for linha in list(por_ean.values()) + list(por_descricao.values())})
    conflitos = set()
    pendentes = set()
    if ids:
        conflitos = {
            linha["produto_id"]
            for linha in conexao.execute(
                "SELECT DISTINCT produto_id FROM conflitos WHERE produto_id = ANY(%s)",
                (ids,),
            ).fetchall()
        }
        pendentes = {
            linha["produto_id"]
            for linha in conexao.execute(
                """
                SELECT DISTINCT produto_id FROM sugestoes
                WHERE situacao = 'pendente' AND produto_id = ANY(%s)
                """,
                (ids,),
            ).fetchall()
        }
    return {"ean": por_ean, "descricao": por_descricao, "conflitos": conflitos, "pendentes": pendentes}


def _um(conexao, item, encontrados, regras, oficiais, regime, fabrica) -> dict:
    if not isinstance(item, dict):
        return {"grupo": None, "ausente": {"codigo": "", "descricao": "", "motivo": "invalido"}}
    codigo = str(item.get("codigo") or "").strip()
    descricao = str(item.get("descricao") or "").strip()
    interno = _interno(item)
    ean = None if interno else normalizar_ean(codigo)
    if ean is None and not descricao:
        return {"grupo": None, "ausente": {"codigo": codigo, "descricao": descricao, "motivo": "invalido"}}
    produto = None
    if ean:
        produto = encontrados["ean"].get(ean)
    if produto is None and descricao:
        produto = encontrados["descricao"].get(normalizar_texto(descricao))
    if produto is None:
        if descricao:
            _registrar_descricao(conexao, descricao, ean, item.get("ncm"))
        return {"grupo": None, "ausente": {"codigo": codigo, "descricao": descricao, "motivo": "ausente"}}
    if produto["id"] in encontrados["conflitos"]:
        return {"grupo": None, "ausente": {"codigo": codigo, "descricao": descricao, "motivo": "conflito"}}
    if produto["id"] in encontrados["pendentes"]:
        return {"grupo": None, "ausente": {"codigo": codigo, "descricao": descricao, "motivo": "pendente"}}
    tributos = montar_tributos(produto, regras, oficiais, regime, fabrica)
    if tributos is None:
        return {"grupo": None, "ausente": {"codigo": codigo, "descricao": descricao, "motivo": "sem_regra"}}
    grupo = {
        "codigo": codigo,
        "descricao": produto["descricao"],
        "ncm": produto["ncm"],
        "cest": produto.get("cest") or "",
        "vigencia": tributos.pop("vigencia"),
        **tributos,
    }
    if produto.get("ean"):
        grupo["ean"] = produto["ean"].zfill(14)
    return {"grupo": grupo, "ausente": None}


def _registrar_descricao(conexao, descricao: str, ean: str | None, ncm) -> None:
    salvar_sugestao(
        conexao,
        produto_id=None,
        ean=ean,
        descricao=descricao,
        descricao_norm=normalizar_texto(descricao),
        ncm_atual=codigo_ncm(ncm) if ncm else None,
        ncm_sugerido=None,
        cclass_sugerido=None,
        analise=None,
        duvida=False,
    )


def _interno(item: dict) -> bool:
    marca = str(item.get("codInterno") or "").strip().upper()
    return marca == "S"
