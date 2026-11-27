from io import BytesIO

import openpyxl
from openpyxl.styles import Font

from ajuste_fiscal.aplicar import Resultado


def gerar_relatorio(resultado: Resultado) -> bytes:
    livro = openpyxl.Workbook()
    resumo = livro.active
    resumo.title = "Resumo"
    _cabecalho(resumo, ["Indicador", "Quantidade"])
    for nome, valor in (
        ("Produtos lidos na contabilidade", resultado.resumo["lidos"]),
        ("Cadastro definitivo (Correto e Incorreto)", resultado.resumo["definitivos"]),
        ("Linhas no CSV do sistema", resultado.resumo["linhas_csv"]),
        ("Aplicados", resultado.resumo["aplicados"]),
        ("Ambíguos", resultado.resumo["ambiguos"]),
        ("Sem correspondência", resultado.resumo["sem_par"]),
        ("Excluídos da gravação", resultado.resumo["excluidos"]),
        ("Encontrados com campo vazio ou inválido", resultado.resumo["invalidos"]),
    ):
        resumo.append([nome, valor])
    _larguras(resumo, [52, 16])

    aplicados = livro.create_sheet("Aplicados")
    _cabecalho(
        aplicados,
        [
            "Código contabilidade",
            "Identificador sistema",
            "Descrição contabilidade",
            "Descrição sistema",
            "Status",
            "Casamento",
            "NCM anterior",
            "NCM novo",
            "Classif. NFe anterior",
            "Classif. NFe nova",
            "Classif. NFCe anterior",
            "Classif. NFCe nova",
            "Avisos",
        ],
    )
    for item in resultado.aplicados:
        aplicados.append(
            [
                item.item.codigo,
                item.identificador,
                item.item.descricao,
                item.descricao_sistema,
                item.item.status,
                item.tipo_casamento,
                item.ncm_anterior,
                item.ncm_novo,
                item.nfe_anterior,
                item.nfe_novo,
                item.nfce_anterior,
                item.nfce_novo,
                "; ".join(item.avisos),
            ]
        )
    _larguras(aplicados, [22, 22, 40, 40, 18, 14, 16, 16, 22, 20, 22, 20, 40])

    _aba_ocorrencias(
        livro.create_sheet("Ambiguos"),
        resultado.ambiguos,
        com_candidatos=True,
    )
    _aba_ocorrencias(livro.create_sheet("Sem correspondencia"), resultado.sem_par, com_candidatos=False)
    _aba_ocorrencias(livro.create_sheet("Excluidos"), resultado.excluidos, com_candidatos=False)
    _aba_ocorrencias(livro.create_sheet("Campos invalidos"), resultado.invalidos, com_candidatos=False)

    buffer = BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def _aba_ocorrencias(aba, ocorrencias, com_candidatos: bool) -> None:
    colunas = ["Código", "Descrição", "Status", "NCM para cadastro", "cClassTrib", "Motivo"]
    if com_candidatos:
        colunas.append("Candidatos no sistema")
    _cabecalho(aba, colunas)
    for ocorrencia in ocorrencias:
        linha = [
            ocorrencia.item.codigo,
            ocorrencia.item.descricao,
            ocorrencia.item.status,
            ocorrencia.item.ncm or "",
            ocorrencia.item.cclass or "",
            ocorrencia.motivo,
        ]
        if com_candidatos:
            linha.append("; ".join(ocorrencia.candidatos))
        aba.append(linha)
    larguras = [18, 42, 28, 20, 16, 55]
    if com_candidatos:
        larguras.append(55)
    _larguras(aba, larguras)


def _cabecalho(aba, colunas: list[str]) -> None:
    aba.append(colunas)
    for celula in aba[1]:
        celula.font = Font(bold=True)


def _larguras(aba, larguras: list[int]) -> None:
    for indice, largura in enumerate(larguras, start=1):
        aba.column_dimensions[openpyxl.utils.get_column_letter(indice)].width = largura
