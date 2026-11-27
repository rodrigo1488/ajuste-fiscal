"""Grava na base os produtos definitivos, sem gerar o CSV do sistema."""

from io import BytesIO

import openpyxl

from ajuste_fiscal.aplicar import Resultado
from ajuste_fiscal.banco import registrar
from ajuste_fiscal.contabilidade import ItemContabilidade, ler_contabilidade
from ajuste_fiscal.laudo import ler_laudo
from ajuste_fiscal.normalizar import (
    codigo_cclass,
    codigo_fiscal,
    codigo_ncm,
    normalizar_texto,
    numero_fiscal,
    tipo_status,
)
from ajuste_fiscal.sistema import planilha_vazia

ABA = "Importacao"


def importar_para_base(origem, nome_arquivo: str = "", url: str | None = None):
    """Lê a aba Importacao, a auditoria ou o PDF e persiste Correto e Incorreto."""
    itens = ler_importacao(origem)
    resultado = Resultado(
        itens=itens,
        planilha=planilha_vazia(),
        aplicados=[],
        ambiguos=[],
        sem_par=[],
        excluidos=[],
        invalidos=[],
        csv_bytes=b"",
    )
    return registrar(resultado, nome_arquivo, url)


def ler_importacao(origem) -> list[ItemContabilidade]:
    dados = _bytes(origem)
    if dados.startswith(b"%PDF"):
        return ler_laudo(BytesIO(dados))
    livro = openpyxl.load_workbook(BytesIO(dados), data_only=True, read_only=True)
    try:
        if "Produtos auditados" in livro.sheetnames:
            usar_auditoria = True
        elif ABA in livro.sheetnames:
            usar_auditoria = False
        else:
            raise ValueError(
                "A planilha precisa da aba Importacao ou da aba Produtos auditados."
            )
    finally:
        livro.close()
    if usar_auditoria:
        return ler_contabilidade(BytesIO(dados))
    return _ler_aba(BytesIO(dados))


def _ler_aba(origem) -> list[ItemContabilidade]:
    livro = openpyxl.load_workbook(origem, data_only=True, read_only=True)
    try:
        aba = livro[ABA]
        linhas = aba.iter_rows(values_only=True)
        try:
            cabecalhos = [normalizar_texto(valor) for valor in next(linhas)]
        except StopIteration:
            raise ValueError("A aba Importacao está vazia.")
        colunas = _mapa(cabecalhos)
        itens = []
        for numero, valores in enumerate(linhas, start=2):
            if valores is None or all(valor is None or str(valor).strip() == "" for valor in valores):
                continue
            descricao = _texto(_pegar(valores, colunas["descricao"]))
            codigo = _texto(_pegar(valores, colunas.get("codigo", -1)))
            status = _texto(_pegar(valores, colunas["status"]))
            if not descricao and not codigo and not status:
                continue
            ncm = codigo_ncm(_pegar(valores, colunas["ncm"]))
            cclass = codigo_cclass(_pegar(valores, colunas["cclass"]))
            incorreto = normalizar_texto(status) == "INCORRETO"
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
                    aviso_ncm=None,
                    aviso_cclass=None,
                    ncm_anterior=codigo_ncm(_pegar(valores, colunas.get("ncm_antigo", -1))) if incorreto else None,
                    cst_ibs_cbs=codigo_fiscal(_pegar(valores, colunas.get("cst", -1)), 3),
                    reducao_cbs=numero_fiscal(_pegar(valores, colunas.get("red_cbs", -1))),
                    reducao_ibs_uf=numero_fiscal(_pegar(valores, colunas.get("red_uf", -1))),
                    reducao_ibs_mun=numero_fiscal(_pegar(valores, colunas.get("red_mun", -1))),
                    imposto_seletivo=_sim_nao(_pegar(valores, colunas.get("seletivo", -1))),
                )
            )
        return itens
    finally:
        livro.close()


def _mapa(cabecalhos: list[str]) -> dict[str, int]:
    mapa = {
        "descricao": _indice(cabecalhos, lambda nome: nome == "DESCRICAO"),
        "ncm": _indice(cabecalhos, lambda nome: nome == "NCM"),
        "cclass": _indice(cabecalhos, lambda nome: "CCLASS" in nome or nome == "CCLASSTRIB"),
        "status": _indice(cabecalhos, lambda nome: "STATUS" in nome),
    }
    nomes = {
        "descricao": "descricao",
        "ncm": "ncm",
        "cclass": "cclasstrib",
        "status": "status_auditoria",
    }
    faltando = [nomes[chave] for chave, indice in mapa.items() if indice < 0]
    if faltando:
        raise ValueError("A aba Importacao precisa das colunas: " + ", ".join(faltando) + ".")
    mapa.update(
        {
            "codigo": _indice(cabecalhos, lambda nome: nome == "CODIGO" or nome.startswith("CODIGO ")),
            "cst": _indice(cabecalhos, lambda nome: "CST" in nome and "IBS" in nome),
            "red_cbs": _indice(cabecalhos, lambda nome: "RED" in nome and "CBS" in nome),
            "red_uf": _indice(cabecalhos, lambda nome: "RED" in nome and "UF" in nome),
            "red_mun": _indice(cabecalhos, lambda nome: "RED" in nome and "MUN" in nome),
            "seletivo": _indice(cabecalhos, lambda nome: "SELETIVO" in nome),
            "ncm_antigo": _indice(cabecalhos, lambda nome: "NCM" in nome and "ANTIGO" in nome),
        }
    )
    return mapa


def _indice(cabecalhos: list[str], predicado) -> int:
    for indice, nome in enumerate(cabecalhos):
        if predicado(nome):
            return indice
    return -1


def _pegar(valores, indice: int):
    if indice < 0 or indice >= len(valores):
        return None
    return valores[indice]


def _texto(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    if isinstance(valor, int) and not isinstance(valor, bool):
        return str(valor)
    return str(valor).strip()


def _sim_nao(valor) -> str | None:
    texto = normalizar_texto(valor)
    if texto in {"SIM", "S"}:
        return "Sim"
    if texto in {"NAO", "N"}:
        return "Não"
    return None


def _bytes(origem) -> bytes:
    if isinstance(origem, (bytes, bytearray)):
        return bytes(origem)
    posicao = origem.tell() if hasattr(origem, "tell") else None
    dados = origem.read()
    if hasattr(origem, "seek") and posicao is not None:
        origem.seek(posicao)
    return dados
