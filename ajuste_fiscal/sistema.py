import csv
from dataclasses import dataclass

from ajuste_fiscal.normalizar import normalizar_texto


@dataclass
class PlanilhaSistema:
    encoding: str
    quebra: str
    termina_com_quebra: bool
    cabecalho_cru: str
    campos_cabecalho: list[str]
    linhas: list["LinhaSistema"]
    indice_descricao: int
    indice_ncm: int
    indice_nfe: int
    indice_nfce: int
    indice_identificador: int
    indices: dict[str, int]


@dataclass
class LinhaSistema:
    numero: int
    cru: str
    campos: list[str]
    identificador: str
    descricao: str
    descricao_norm: str


def planilha_vazia() -> PlanilhaSistema:
    return PlanilhaSistema(
        encoding="utf-8",
        quebra="\n",
        termina_com_quebra=True,
        cabecalho_cru="",
        campos_cabecalho=[],
        linhas=[],
        indice_descricao=-1,
        indice_ncm=-1,
        indice_nfe=-1,
        indice_nfce=-1,
        indice_identificador=-1,
        indices={},
    )


def decodificar(conteudo: bytes) -> tuple[str, str]:
    if conteudo.startswith(b"\xef\xbb\xbf"):
        return conteudo[3:].decode("utf-8"), "utf-8-sig"
    try:
        texto = conteudo.decode("utf-8")
    except UnicodeDecodeError:
        texto = None
    if texto is not None and _parece_planilha(texto):
        return texto, "utf-8"
    return conteudo.decode("latin-1"), "latin-1"


def ler_sistema(conteudo: bytes) -> PlanilhaSistema:
    texto, encoding = decodificar(conteudo)
    if not texto.strip():
        raise ValueError("O CSV do sistema está vazio.")
    quebra = "\r\n" if "\r\n" in texto else "\n"
    termina_com_quebra = texto.endswith("\n") or texto.endswith("\r")
    cruas = texto.splitlines()
    if not cruas or not cruas[0].strip():
        raise ValueError("O CSV do sistema não tem cabeçalho.")
    campos_cabecalho = _ler_campos(cruas[0])
    nomes = [normalizar_texto(campo) for campo in campos_cabecalho]
    indices = {
        "descricao": _indice(nomes, lambda nome: nome == "DESCRICAO"),
        "ncm": _indice(nomes, lambda nome: nome == "NCM"),
        "nfe": _indice(
            nomes,
            lambda nome: "CLASSIF" in nome and "NFE" in nome and "NFCE" not in nome,
        ),
        "nfce": _indice(nomes, lambda nome: "CLASSIF" in nome and "NFCE" in nome),
        "identificador": _indice(nomes, lambda nome: nome == "IDENTIFICADOR"),
        "ean": _indice(nomes, lambda nome: nome == "EAN" or "BARRAS" in nome),
        "cest": _indice(nomes, lambda nome: nome == "CEST"),
        "cfop_pdv": _indice(nomes, lambda nome: "CFOP" in nome and "PDV" in nome),
        "cfop_nfe": _indice(nomes, lambda nome: "CFOP" in nome and "NFE" in nome),
        "cst_icms": _indice(nomes, lambda nome: nome == "CST NFE"),
        "csosn": _indice(nomes, lambda nome: nome == "CSOSN NFE"),
        "cst_pis": _indice(nomes, lambda nome: nome == "CST PIS"),
        "aliq_pis": _indice(nomes, lambda nome: "ALIQ" in nome and "PIS" in nome),
        "cst_cofins": _indice(nomes, lambda nome: nome == "CST COFINS"),
        "aliq_cofins": _indice(nomes, lambda nome: "ALIQ" in nome and "COFINS" in nome),
        "cst_ipi": _indice(nomes, lambda nome: nome == "CST IPI"),
        "aliq_ipi": _indice(nomes, lambda nome: "ALIQ" in nome and "IPI" in nome),
        "natureza": _indice(nomes, lambda nome: nome == "NATUREZA" or nome.startswith("NATUREZA ")),
    }
    nomes = {
        "descricao": "Descricao",
        "ncm": "NCM",
        "nfe": "Classif. tributária NFe",
        "nfce": "Classif. tributária NFCe",
    }
    faltando = [nomes[chave] for chave, indice in indices.items() if indice < 0 and chave in nomes]
    if faltando:
        raise ValueError("O CSV do sistema precisa das colunas: " + ", ".join(faltando) + ".")
    linhas = []
    for numero, cru in enumerate(cruas[1:], start=2):
        if cru.strip() == "":
            linhas.append(
                LinhaSistema(
                    numero=numero,
                    cru=cru,
                    campos=[],
                    identificador="",
                    descricao="",
                    descricao_norm="",
                )
            )
            continue
        campos = _ler_campos(cru)
        identificador = _campo(campos, indices["identificador"])
        descricao = _campo(campos, indices["descricao"])
        linhas.append(
            LinhaSistema(
                numero=numero,
                cru=cru,
                campos=campos,
                identificador=identificador,
                descricao=descricao,
                descricao_norm=normalizar_texto(descricao),
            )
        )
    return PlanilhaSistema(
        encoding=encoding,
        quebra=quebra,
        termina_com_quebra=termina_com_quebra,
        cabecalho_cru=cruas[0],
        campos_cabecalho=campos_cabecalho,
        linhas=linhas,
        indice_descricao=indices["descricao"],
        indice_ncm=indices["ncm"],
        indice_nfe=indices["nfe"],
        indice_nfce=indices["nfce"],
        indice_identificador=indices["identificador"],
        indices=indices,
    )


def gravar_sistema(planilha: PlanilhaSistema, alteracoes: dict[int, list[str]]) -> bytes:
    """Regrava o CSV. Linhas fora de `alteracoes` permanecem com o texto original."""
    saida = [planilha.cabecalho_cru]
    for linha in planilha.linhas:
        if linha.numero in alteracoes:
            saida.append(_gravar_campos(alteracoes[linha.numero]))
        else:
            saida.append(linha.cru)
    texto = planilha.quebra.join(saida)
    if planilha.termina_com_quebra:
        texto += planilha.quebra
    if planilha.encoding == "utf-8-sig":
        return texto.encode("utf-8-sig")
    return texto.encode(planilha.encoding)


def _parece_planilha(texto: str) -> bool:
    primeira = texto.splitlines()[0] if texto else ""
    nome = normalizar_texto(primeira)
    return "NCM" in nome and "DESCRICAO" in nome


def _indice(nomes: list[str], predicado) -> int:
    for indice, nome in enumerate(nomes):
        if predicado(nome):
            return indice
    return -1


def _ler_campos(linha: str) -> list[str]:
    return next(csv.reader([linha], delimiter="|"))


def _campo(campos: list[str], indice: int) -> str:
    if indice < 0 or indice >= len(campos):
        return ""
    return campos[indice].strip()


def _gravar_campos(campos: list[str]) -> str:
    from io import StringIO

    buffer = StringIO()
    escritor = csv.writer(buffer, delimiter="|", lineterminator="")
    escritor.writerow(campos)
    return buffer.getvalue()
