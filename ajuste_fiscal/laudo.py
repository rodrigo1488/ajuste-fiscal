"""Lê o laudo em PDF da contabilidade pelo texto do Anexo I, sem interpretação por IA."""

import re
from io import BytesIO

from pypdf import PdfReader

from ajuste_fiscal.contabilidade import ItemContabilidade
from ajuste_fiscal.normalizar import codigo_cclass, codigo_fiscal, codigo_ncm, normalizar_texto, tipo_status

_NCM = re.compile(r"\d{4}\.\d{2}\.\d{2}")
_STATUS = re.compile(r"Não é\s+mercadoria|Incorreto|Correto|Revisar")
_INICIO_LINHA = re.compile(r"^(\d+)\s+(\d+)\s+(.*)$")
_CLASSE = re.compile(r"\b(\d{3})\s+(\d{6})\b")
_REDUCAO = re.compile(r"(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)")
_SELETIVO = re.compile(r"\b(Sim|Não|Nao)\b")
_LINHA_IGNORADA = re.compile(
    r"^(ORCCA CONTABILIDADE|Documento confidencial|# Código Descrição|Alíquotas de referência)",
    re.IGNORECASE,
)
_FRAGMENTO_CABECALHO = {
    "informado",
    "ncm sugerido status cst cclasstrib % red.",
    "cbs/ibs",
    "is cnae ação",
    "is cnae acao",
}


def ler_laudo(origem) -> list[ItemContabilidade]:
    if isinstance(origem, (bytes, bytearray)):
        origem = BytesIO(origem)
    leitor = PdfReader(origem)
    if leitor.is_encrypted:
        raise ValueError("O PDF do laudo está protegido por senha.")
    texto = "\n".join((pagina.extract_text() or "") for pagina in leitor.pages)
    return itens_do_texto(texto)


def itens_do_texto(texto: str) -> list[ItemContabilidade]:
    anexo = _recorte_anexo(texto)
    blocos = _blocos_do_anexo(anexo)
    if not blocos:
        raise ValueError(
            "O PDF não tem o Anexo I com a lista de produtos (código, NCM e cClassTrib)."
        )
    return [_item(bloco) for bloco in blocos]


def _recorte_anexo(texto: str) -> str:
    inicio = texto.find("Anexo I")
    if inicio < 0:
        inicio = texto.lower().find("lista completa de produtos")
    if inicio < 0:
        raise ValueError(
            "O PDF não tem o Anexo I com a lista de produtos (código, NCM e cClassTrib)."
        )
    trecho = texto[inicio:]
    fim = trecho.find("Fundamentação legal")
    if fim > 0:
        trecho = trecho[:fim]
    return trecho


def _blocos_do_anexo(texto: str) -> list[dict]:
    blocos = []
    atual = None
    esperado = 1
    for bruta in texto.splitlines():
        linha = " ".join(bruta.replace("\xa0", " ").split())
        if not linha or _ignorar(linha):
            continue
        encontrado = _INICIO_LINHA.match(linha)
        if encontrado and int(encontrado.group(1)) == esperado:
            if atual is not None:
                blocos.append(atual)
            atual = {
                "linha": esperado,
                "codigo": encontrado.group(2),
                "partes": [encontrado.group(3)],
            }
            esperado += 1
            continue
        if atual is not None:
            atual["partes"].append(linha)
    if atual is not None:
        blocos.append(atual)
    return blocos


def _ignorar(linha: str) -> bool:
    if _LINHA_IGNORADA.match(linha):
        return True
    return normalizar_texto(linha).lower() in _FRAGMENTO_CABECALHO or normalizar_texto(linha) in {
        "INFORMADO",
        "NCM SUGERIDO STATUS CST CCLASSTRIB RED",
        "CBS IBS",
        "IS CNAE ACAO",
    }


def _item(bloco: dict) -> ItemContabilidade:
    texto = " ".join(bloco["partes"])
    texto = texto.replace("\xa0", " ")
    status_encontrado = _STATUS.search(texto)
    if status_encontrado is None:
        raise ValueError(
            f"Não foi possível ler o status do produto {bloco['codigo']} no Anexo I do PDF."
        )
    status = re.sub(r"\s+", " ", status_encontrado.group(0))
    antes = texto[: status_encontrado.start()]
    depois = texto[status_encontrado.end() :]
    ncms = _NCM.findall(antes)
    primeiro_ncm = _NCM.search(antes)
    descricao = antes[: primeiro_ncm.start()] if primeiro_ncm else antes
    descricao = re.sub(r"\s+", " ", descricao).strip(" -—–")
    ncm_cadastro = _ncm_para_cadastro(status, ncms)
    classe = _CLASSE.search(depois)
    cclass = codigo_cclass(classe.group(2)) if classe else None
    cst_ibs = codigo_fiscal(classe.group(1), 3) if classe else None
    reducao = _REDUCAO.search(depois)
    reducao_cbs = reducao.group(1).replace(",", ".") if reducao else None
    reducao_ibs = reducao.group(2).replace(",", ".") if reducao else None
    seletivo = None
    if reducao:
        marca = _SELETIVO.search(depois, reducao.end())
        if marca:
            seletivo = "Sim" if marca.group(1) == "Sim" else "Não"
    aviso_ncm = None
    aviso_cclass = None
    definitivo = tipo_status(status) == "definitivo"
    if definitivo and ncm_cadastro is None:
        aviso_ncm = "NCM para cadastro vazio"
    if definitivo and cclass is None:
        aviso_cclass = "cClassTrib vazio"
    ncm_anterior = None
    if normalizar_texto(status) == "INCORRETO" and ncms:
        ncm_anterior = codigo_ncm(ncms[0])
    return ItemContabilidade(
        linha=bloco["linha"],
        codigo=bloco["codigo"],
        descricao=descricao,
        descricao_norm=normalizar_texto(descricao),
        status=status,
        definitivo=definitivo,
        ncm=ncm_cadastro,
        cclass=cclass,
        aviso_ncm=aviso_ncm,
        aviso_cclass=aviso_cclass,
        ncm_anterior=ncm_anterior,
        cst_ibs_cbs=cst_ibs,
        reducao_cbs=reducao_cbs,
        reducao_ibs_uf=reducao_ibs,
        reducao_ibs_mun=reducao_ibs,
        imposto_seletivo=seletivo,
    )


def _ncm_para_cadastro(status: str, ncms: list[str]) -> str | None:
    if status.startswith("Não é"):
        return None
    if status == "Incorreto":
        escolhido = ncms[1] if len(ncms) > 1 else None
    elif status == "Correto":
        escolhido = ncms[0] if ncms else None
    else:
        escolhido = ncms[1] if len(ncms) > 1 else (ncms[0] if ncms else None)
    return codigo_ncm(escolhido) if escolhido else None
