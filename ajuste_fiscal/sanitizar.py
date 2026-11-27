"""Lê o CSV do sistema, completa a base e devolve o cadastro sanitizado."""

from collections import Counter
from dataclasses import dataclass

from ajuste_fiscal.banco import (
    _observado,
    _preenchido,
    completar_observacao,
    conectar,
    garantir_schema,
    salvar_sugestao,
    url_banco,
)
from ajuste_fiscal.normalizar import codigo_ncm, normalizar_ean
from ajuste_fiscal.sistema import gravar_sistema, ler_sistema

_CAMPOS = (
    ("ncm", "ncm"),
    ("cest", "cest"),
    ("nfe", "cclass_trib"),
    ("nfce", "cclass_trib"),
)


@dataclass
class ResultadoSanitizacao:
    csv_bytes: bytes
    charset: str
    lidos: int
    sanitizados: int
    conformes: int
    sem_par: int
    conflitos: int
    pendentes: int
    saneamento_id: int


def sanitizar_csv(conteudo: bytes, nome_arquivo: str = "", url: str | None = None) -> ResultadoSanitizacao:
    """Casa cada linha com a base e grava NCM, CEST e cClassTrib confirmados no CSV."""
    if conteudo.startswith(b"PK") or conteudo.startswith(b"%PDF"):
        raise ValueError("Este campo recebe o CSV exportado pelo sistema, separado por |.")
    planilha = ler_sistema(conteudo)
    linhas = [linha for linha in planilha.linhas if linha.campos and (linha.descricao or _ean(planilha, linha))]
    if not linhas:
        raise ValueError("O CSV do sistema não tem produtos.")
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        encontrados = _buscar(conexao, planilha, linhas)
        alteracoes = {}
        registros = []
        for linha in linhas:
            ean = _ean(planilha, linha)
            produto = encontrados["ean"].get(ean) if ean else None
            if produto is None and linha.descricao_norm:
                produto = encontrados["descricao"].get(linha.descricao_norm)
            campos = list(linha.campos)
            ncm_original = _celula(planilha, campos, "ncm")
            cest_original = _celula(planilha, campos, "cest")
            cclass_original = _celula(planilha, campos, "nfe")
            descricao = linha.descricao or (produto["descricao"] if produto else "") or ean or "Sem descrição"
            if produto is None:
                _registrar_ausente(conexao, linha, ean, ncm_original)
                registros.append(
                    _registro(linha, descricao, ean, ncm_original, ncm_original, cest_original, cest_original, cclass_original, cclass_original, "sem_par")
                )
                continue
            if produto["id"] in encontrados["conflitos"]:
                registros.append(
                    _registro(linha, descricao, ean, ncm_original, ncm_original, cest_original, cest_original, cclass_original, cclass_original, "conflito")
                )
                continue
            if produto["id"] in encontrados["pendentes"]:
                registros.append(
                    _registro(linha, descricao, ean, ncm_original, ncm_original, cest_original, cest_original, cclass_original, cclass_original, "pendente")
                )
                continue
            completar_observacao(conexao, produto, _observado(planilha, linha), nome_arquivo)
            mudou = _aplicar(campos, planilha, produto)
            if mudou:
                alteracoes[linha.numero] = campos
            registros.append(
                _registro(
                    linha,
                    descricao,
                    ean,
                    ncm_original,
                    _celula(planilha, campos, "ncm"),
                    cest_original,
                    _celula(planilha, campos, "cest"),
                    cclass_original,
                    _celula(planilha, campos, "nfe"),
                    "sanitizado" if mudou else "conforme",
                )
            )
        contagem = Counter(item["situacao"] for item in registros)
        conexao.execute("DELETE FROM cadastros")
        conexao.execute("DELETE FROM saneamentos")
        cabeca = conexao.execute(
            """
            INSERT INTO saneamentos (
                arquivo, lidos, sanitizados, conformes, sem_par, conflitos, pendentes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                nome_arquivo or None,
                len(registros),
                contagem["sanitizado"],
                contagem["conforme"],
                contagem["sem_par"],
                contagem["conflito"],
                contagem["pendente"],
            ),
        ).fetchone()
        for item in registros:
            conexao.execute(
                """
                INSERT INTO cadastros (
                    saneamento_id, identificador, descricao, ean, ncm_original, ncm,
                    cest_original, cest, cclass_original, cclass, situacao
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cabeca["id"],
                    item["identificador"],
                    item["descricao"],
                    item["ean"],
                    item["ncm_original"],
                    item["ncm"],
                    item["cest_original"],
                    item["cest"],
                    item["cclass_original"],
                    item["cclass"],
                    item["situacao"],
                ),
            )
        conexao.commit()
    return ResultadoSanitizacao(
        csv_bytes=gravar_sistema(planilha, alteracoes),
        charset="utf-8" if planilha.encoding.startswith("utf-8") else "latin-1",
        lidos=len(registros),
        sanitizados=contagem["sanitizado"],
        conformes=contagem["conforme"],
        sem_par=contagem["sem_par"],
        conflitos=contagem["conflito"],
        pendentes=contagem["pendente"],
        saneamento_id=cabeca["id"],
    )


def _buscar(conexao, planilha, linhas) -> dict:
    eans = []
    normas = []
    for linha in linhas:
        ean = _ean(planilha, linha)
        if ean:
            eans.append(ean)
        if linha.descricao_norm:
            normas.append(linha.descricao_norm)
    por_ean = {
        linha["ean"]: linha
        for linha in (
            conexao.execute("SELECT * FROM produtos WHERE ean = ANY(%s)", (list(set(eans)),)).fetchall() if eans else []
        )
    }
    por_descricao = {
        linha["descricao_norm"]: linha
        for linha in (
            conexao.execute(
                "SELECT * FROM produtos WHERE descricao_norm = ANY(%s)",
                (list(set(normas)),),
            ).fetchall()
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


def _aplicar(campos: list[str], planilha, produto: dict) -> bool:
    mudou = False
    for chave, coluna in _CAMPOS:
        valor = produto.get(coluna)
        if not _preenchido(valor):
            continue
        indice = planilha.indices.get(chave, -1)
        if indice < 0:
            continue
        while len(campos) <= indice:
            campos.append("")
        texto = str(valor).strip()
        if campos[indice].strip() == texto:
            continue
        campos[indice] = texto
        mudou = True
    return mudou


def _registrar_ausente(conexao, linha, ean: str | None, ncm: str) -> None:
    if not linha.descricao and not ean:
        return
    salvar_sugestao(
        conexao,
        produto_id=None,
        ean=ean,
        descricao=linha.descricao or ean or "",
        descricao_norm=linha.descricao_norm or (ean or ""),
        ncm_atual=codigo_ncm(ncm),
        ncm_sugerido=None,
        cclass_sugerido=None,
        analise=None,
        duvida=False,
    )


def _registro(linha, descricao, ean, ncm_original, ncm, cest_original, cest, cclass_original, cclass, situacao) -> dict:
    return {
        "identificador": linha.identificador or None,
        "descricao": descricao,
        "ean": ean,
        "ncm_original": ncm_original or None,
        "ncm": ncm or None,
        "cest_original": cest_original or None,
        "cest": cest or None,
        "cclass_original": cclass_original or None,
        "cclass": cclass or None,
        "situacao": situacao,
    }


def _ean(planilha, linha) -> str | None:
    return normalizar_ean(_celula(planilha, linha.campos, "ean"))


def _celula(planilha, campos, chave: str) -> str:
    indice = planilha.indices.get(chave, -1)
    if indice < 0 or indice >= len(campos):
        return ""
    return campos[indice].strip()
