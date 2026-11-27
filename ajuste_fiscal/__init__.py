"""Ajuste de NCM e classificação tributária para o CSV de importação do sistema."""

import os
from io import BytesIO

from ajuste_fiscal.aplicar import aplicar
from ajuste_fiscal.banco import ResumoColeta, registrar
from ajuste_fiscal.contabilidade import ler_contabilidade
from ajuste_fiscal.importacao import importar_para_base, ler_importacao
from ajuste_fiscal.laudo import ler_laudo
from ajuste_fiscal.relatorio import gerar_relatorio
from ajuste_fiscal.sistema import ler_sistema

__all__ = [
    "aplicar",
    "gerar_relatorio",
    "importar_para_base",
    "ler_contabilidade",
    "ler_importacao",
    "ler_laudo",
    "ler_origem",
    "ler_sistema",
    "processar",
]


def processar(planilha_contabilidade, csv_sistema: bytes, nome_arquivo: str = ""):
    itens = ler_origem(planilha_contabilidade)
    planilha = ler_sistema(csv_sistema)
    resultado = aplicar(itens, planilha)
    resultado.coleta = _coletar(resultado, nome_arquivo)
    relatorio = gerar_relatorio(resultado)
    return resultado, relatorio


def _coletar(resultado, nome_arquivo: str) -> ResumoColeta:
    if os.environ.get("AJUSTE_GRAVAR_BASE", "1") == "0":
        return ResumoColeta()
    try:
        return registrar(resultado, nome_arquivo)
    except Exception as exc:
        return ResumoColeta(erro=str(exc))


def ler_origem(origem):
    """Aceita a planilha .xlsx ou o laudo .pdf da contabilidade."""
    if isinstance(origem, (bytes, bytearray)):
        dados = bytes(origem)
    else:
        posicao = origem.tell() if hasattr(origem, "tell") else None
        dados = origem.read()
        if hasattr(origem, "seek") and posicao is not None:
            origem.seek(posicao)
    if dados.startswith(b"%PDF"):
        return ler_laudo(BytesIO(dados))
    return ler_contabilidade(BytesIO(dados))
