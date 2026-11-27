from dataclasses import dataclass

import openpyxl

from ajuste_fiscal.normalizar import (
    codigo_cclass,
    codigo_cest,
    codigo_cfop,
    codigo_fiscal,
    codigo_ncm,
    normalizar_texto,
    numero_fiscal,
    partir_natureza,
    tipo_status,
)

ABA = "Produtos auditados"


@dataclass
class ItemContabilidade:
    linha: int
    codigo: str
    descricao: str
    descricao_norm: str
    status: str
    definitivo: bool
    ncm: str | None
    cclass: str | None
    aviso_ncm: str | None
    aviso_cclass: str | None
    ncm_anterior: str | None = None
    cest: str | None = None
    cst_ibs_cbs: str | None = None
    descricao_cclass: str | None = None
    reducao_cbs: str | None = None
    reducao_ibs_uf: str | None = None
    reducao_ibs_mun: str | None = None
    imposto_seletivo: str | None = None
    natureza_codigo: str | None = None
    natureza_descricao: str | None = None
    cfop_pdv: str | None = None
    cfop_nfe: str | None = None
    cst_icms: str | None = None
    analise: str | None = None


def _cabecalho(valor) -> str:
    return normalizar_texto(valor)


def _indice(cabecalhos: list[str], predicado) -> int:
    for indice, nome in enumerate(cabecalhos):
        if predicado(nome):
            return indice
    return -1


def _mapa_colunas(cabecalhos: list[str]) -> dict[str, int]:
    mapa = {
        "codigo": _indice(cabecalhos, lambda nome: nome == "CODIGO"),
        "descricao": _indice(cabecalhos, lambda nome: nome == "DESCRICAO"),
        "status": _indice(
            cabecalhos,
            lambda nome: "STATUS" in nome and "NCM" in nome,
        ),
        "ncm": _indice(
            cabecalhos,
            lambda nome: "NCM" in nome and "CADASTRO" in nome,
        ),
        "cclass": _indice(cabecalhos, lambda nome: "CCLASSTRIB" in nome and "DESCRICAO" not in nome),
    }
    mapa.update(_colunas_opcionais(cabecalhos))
    nomes = {
        "codigo": "Código",
        "descricao": "Descrição",
        "status": "ORCCA | Status NCM",
        "ncm": "NCM para cadastro",
        "cclass": "cClassTrib",
    }
    faltando = [nomes[chave] for chave, indice in mapa.items() if chave in nomes and indice < 0]
    if faltando:
        raise ValueError(
            "A aba 'Produtos auditados' precisa das colunas: " + ", ".join(faltando) + "."
        )
    return mapa


def _colunas_opcionais(cabecalhos: list[str]) -> dict[str, int]:
    return {
        "ncm_atual": _indice(cabecalhos, lambda nome: nome == "NCM"),
        "cest": _indice(cabecalhos, lambda nome: nome == "CEST"),
        "cst_ibs": _indice(cabecalhos, lambda nome: "CST" in nome and "IBS" in nome),
        "desc_cclass": _indice(
            cabecalhos, lambda nome: "DESCRICAO" in nome and "CCLASSTRIB" in nome
        ),
        "red_cbs": _indice(cabecalhos, lambda nome: "REDUCAO" in nome and "CBS" in nome),
        "red_uf": _indice(
            cabecalhos, lambda nome: "REDUCAO" in nome and "IBS" in nome and "UF" in nome
        ),
        "red_mun": _indice(
            cabecalhos, lambda nome: "REDUCAO" in nome and "IBS" in nome and "MUN" in nome
        ),
        "seletivo": _indice(cabecalhos, lambda nome: "IMPOSTO" in nome and "SELETIVO" in nome),
        "cfop_pdv": _indice(
            cabecalhos,
            lambda nome: "CFOP" in nome and ("PDV" in nome or "ECF" in nome or "CFE" in nome),
        ),
        "cfop_nfe": _indice(
            cabecalhos,
            lambda nome: "CFOP" in nome and "NFE" in nome and "ECF" not in nome and "CFE" not in nome,
        ),
        "cst_icms": _indice(cabecalhos, lambda nome: nome == "CST"),
        "natureza": _indice(cabecalhos, lambda nome: nome == "NATUREZA" or nome.startswith("NATUREZA ")),
        "analise": _indice(cabecalhos, lambda nome: "ANALISE" in nome),
    }


def _texto_celula(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    if isinstance(valor, int) and not isinstance(valor, bool):
        return str(valor)
    return str(valor).strip()


def ler_contabilidade(origem) -> list[ItemContabilidade]:
    """Lê a aba Produtos auditados. `origem` é caminho ou arquivo binário."""
    livro = openpyxl.load_workbook(origem, data_only=True, read_only=True)
    try:
        if ABA not in livro.sheetnames:
            raise ValueError(
                f"A planilha não tem a aba '{ABA}'. Abas encontradas: "
                + ", ".join(livro.sheetnames)
                + "."
            )
        aba = livro[ABA]
        linhas = aba.iter_rows(values_only=True)
        try:
            primeira = next(linhas)
        except StopIteration:
            raise ValueError(f"A aba '{ABA}' está vazia.")
        cabecalhos = [_cabecalho(valor) for valor in primeira]
        colunas = _mapa_colunas(cabecalhos)
        itens = []
        for numero, valores in enumerate(linhas, start=2):
            if valores is None or all(valor is None or str(valor).strip() == "" for valor in valores):
                continue
            descricao = _texto_celula(_pegar(valores, colunas["descricao"]))
            codigo = _texto_celula(_pegar(valores, colunas["codigo"]))
            status = _texto_celula(_pegar(valores, colunas["status"]))
            if not descricao and not codigo and not status:
                continue
            ncm_bruto = _pegar(valores, colunas["ncm"])
            cclass_bruto = _pegar(valores, colunas["cclass"])
            ncm = codigo_ncm(ncm_bruto)
            cclass = codigo_cclass(cclass_bruto)
            aviso_ncm = None
            aviso_cclass = None
            if ncm is None and _tem_valor(ncm_bruto):
                aviso_ncm = "NCM para cadastro inválido"
            elif ncm is None:
                aviso_ncm = "NCM para cadastro vazio"
            if cclass is None and _tem_valor(cclass_bruto):
                aviso_cclass = "cClassTrib inválido"
            elif cclass is None:
                aviso_cclass = "cClassTrib vazio"
            ncm_atual = codigo_ncm(_pegar(valores, colunas.get("ncm_atual", -1)))
            natureza_codigo, natureza_descricao = partir_natureza(
                _pegar(valores, colunas.get("natureza", -1))
            )
            itens.append(
                ItemContabilidade(
                    linha=numero,
                    codigo=codigo,
                    descricao=descricao,
                    descricao_norm=normalizar_texto(descricao),
                    status=status,
                    definitivo=tipo_status(status) == "definitivo",
                    ncm=ncm,
                    cclass=cclass,
                    aviso_ncm=aviso_ncm,
                    aviso_cclass=aviso_cclass,
                    ncm_anterior=ncm_atual if tipo_status(status) == "definitivo" and normalizar_texto(status) == "INCORRETO" else None,
                    cest=codigo_cest(_pegar(valores, colunas.get("cest", -1))),
                    cst_ibs_cbs=codigo_fiscal(_pegar(valores, colunas.get("cst_ibs", -1)), 3),
                    descricao_cclass=_texto_celula(_pegar(valores, colunas.get("desc_cclass", -1))) or None,
                    reducao_cbs=numero_fiscal(_pegar(valores, colunas.get("red_cbs", -1))),
                    reducao_ibs_uf=numero_fiscal(_pegar(valores, colunas.get("red_uf", -1))),
                    reducao_ibs_mun=numero_fiscal(_pegar(valores, colunas.get("red_mun", -1))),
                    imposto_seletivo=_sim_nao(_pegar(valores, colunas.get("seletivo", -1))),
                    natureza_codigo=natureza_codigo,
                    natureza_descricao=natureza_descricao,
                    cfop_pdv=codigo_cfop(_pegar(valores, colunas.get("cfop_pdv", -1))),
                    cfop_nfe=codigo_cfop(_pegar(valores, colunas.get("cfop_nfe", -1))),
                    cst_icms=codigo_fiscal(_pegar(valores, colunas.get("cst_icms", -1)), 3),
                    analise=_texto_celula(_pegar(valores, colunas.get("analise", -1))) or None,
                )
            )
        return itens
    finally:
        livro.close()


def _pegar(valores, indice: int):
    if indice < 0 or indice >= len(valores):
        return None
    return valores[indice]


def _tem_valor(valor) -> bool:
    if valor is None:
        return False
    return str(valor).strip() != ""


def _sim_nao(valor) -> str | None:
    texto = normalizar_texto(valor)
    if texto in {"SIM", "S"}:
        return "Sim"
    if texto in {"NAO", "N"}:
        return "Não"
    return None
