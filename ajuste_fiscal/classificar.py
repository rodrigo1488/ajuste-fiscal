"""Sugere NCM e cClassTrib com o Gemini e só grava depois da conferência humana."""

import json
import os
import re
from dataclasses import dataclass
from urllib.request import Request, urlopen

from ajuste_fiscal.banco import (
    atualizar_texto_legal,
    conectar,
    garantir_schema,
    salvar_sugestao,
)
from ajuste_fiscal.normalizar import codigo_cclass, codigo_ncm, normalizar_texto

MODELO_PADRAO = "gemini-2.5-flash"
LOTE_CLASSIFICACAO = 8
LIMITE_SUGESTAO = 20
_PARADA = {"PARA", "COM", "SEM", "TIPO", "UNIDADE", "PRODUTO"}


class SemChave(RuntimeError):
    """A conferência pediu uma sugestão e GEMINI_API_KEY não está definida."""


@dataclass
class ResumoConferencia:
    atualizados: int = 0
    sugestoes: int = 0
    sem_chave: int = 0
    rejeitadas: int = 0
    erro: str | None = None

    def como_dict(self) -> dict:
        return {
            "atualizados": self.atualizados,
            "sugestoes": self.sugestoes,
            "sem_chave": self.sem_chave,
            "rejeitadas": self.rejeitadas,
            "erro": self.erro,
        }


@dataclass
class SugestaoModelo:
    ncm: str
    cclass: str
    analise: str
    duvida: bool


def chave_gemini() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def modelo_gemini() -> str:
    return os.environ.get("GEMINI_MODEL", MODELO_PADRAO).strip() or MODELO_PADRAO


def conferir(url: str | None = None, gerar=None) -> ResumoConferencia:
    resumo = ResumoConferencia()
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        ncms = {
            linha["codigo"]
            for linha in conexao.execute("SELECT codigo FROM ncm_oficial").fetchall()
        }
        classes = {
            linha["codigo"]: linha
            for linha in conexao.execute("SELECT * FROM cclass_oficial").fetchall()
        }
        seletivos = {
            linha["ncm"]: linha
            for linha in conexao.execute("SELECT * FROM seletivo_oficial").fetchall()
        }
        vigencia = conexao.execute(
            "SELECT data FROM vigencia_oficial WHERE chave = 'atual'"
        ).fetchone()
        data_tabela = vigencia["data"] if vigencia else None
        if not ncms:
            resumo.erro = "Atualize as tabelas oficiais antes de conferir os cadastros."
            return resumo
        produtos = conexao.execute("SELECT * FROM produtos ORDER BY id").fetchall()
        fila = []
        for produto in produtos:
            divergente = conexao.execute(
                """
                SELECT * FROM sugestoes
                WHERE situacao = 'pendente' AND produto_id = %s
                  AND descricao_norm <> %s AND ncm_sugerido IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (produto["id"], produto["descricao_norm"]),
            ).fetchone()
            extinto = bool(produto["ncm"]) and produto["ncm"] not in ncms
            if not extinto and divergente is None:
                atualizar_texto_legal(
                    conexao,
                    produto,
                    classes.get(produto["cclass_trib"]),
                    seletivos.get(produto["ncm"]),
                    data_tabela,
                )
                resumo.atualizados += 1
                continue
            if gerar is None and not chave_gemini():
                resumo.sem_chave += 1
                continue
            descricao = divergente["descricao"] if divergente is not None else produto["descricao"]
            fila.append((produto, descricao))
        for inicio in range(0, len(fila), LOTE_CLASSIFICACAO):
            bloco = fila[inicio : inicio + LOTE_CLASSIFICACAO]
            try:
                modelos = classificar_lote(
                    conexao,
                    [(descricao, produto["ncm"]) for produto, descricao in bloco],
                    gerar=gerar,
                )
            except SemChave:
                resumo.sem_chave += len(bloco)
                continue
            except Exception as exc:
                resumo.erro = str(exc)
                break
            for (produto, descricao), modelo in zip(bloco, modelos):
                if modelo is None:
                    resumo.rejeitadas += 1
                    continue
                salvar_sugestao(
                    conexao,
                    produto_id=produto["id"],
                    ean=produto["ean"],
                    descricao=descricao,
                    descricao_norm=normalizar_texto(descricao),
                    ncm_atual=produto["ncm"],
                    ncm_sugerido=modelo.ncm,
                    cclass_sugerido=modelo.cclass,
                    analise=modelo.analise,
                    duvida=modelo.duvida,
                )
                resumo.sugestoes += 1
        if resumo.erro is None:
            _classificar_descricoes_novas(conexao, resumo, gerar)
        conexao.commit()
    return resumo


def _classificar_descricoes_novas(conexao, resumo, gerar) -> None:
    orfas = conexao.execute(
        """
        SELECT * FROM sugestoes
        WHERE situacao = 'pendente' AND produto_id IS NULL AND ncm_sugerido IS NULL
        ORDER BY id
        """
    ).fetchall()
    for inicio in range(0, len(orfas), LOTE_CLASSIFICACAO):
        bloco = orfas[inicio : inicio + LOTE_CLASSIFICACAO]
        if gerar is None and not chave_gemini():
            resumo.sem_chave += len(bloco)
            continue
        try:
            modelos = classificar_lote(
                conexao,
                [(linha["descricao"], linha["ncm_atual"]) for linha in bloco],
                gerar=gerar,
            )
        except SemChave:
            resumo.sem_chave += len(bloco)
            continue
        except Exception as exc:
            resumo.erro = str(exc)
            return
        for linha, modelo in zip(bloco, modelos):
            if modelo is None:
                resumo.rejeitadas += 1
                continue
            salvar_sugestao(
                conexao,
                produto_id=None,
                ean=linha["ean"],
                descricao=linha["descricao"],
                descricao_norm=linha["descricao_norm"],
                ncm_atual=linha["ncm_atual"],
                ncm_sugerido=modelo.ncm,
                cclass_sugerido=modelo.cclass,
                analise=modelo.analise,
                duvida=modelo.duvida,
            )
            resumo.sugestoes += 1


def sugerir_publico(descricao: str, ncm_atual: str | None, ean: str | None, gerar=None, url: str | None = None):
    from ajuste_fiscal.banco import consultar_ean
    from ajuste_fiscal.normalizar import normalizar_ean

    if not str(descricao or "").strip():
        return 400, {"erro": "Informe a descrição do produto."}
    if gerar is None and not chave_gemini():
        return 503, {"erro": "A classificação por IA está indisponível."}
    try:
        with conectar(url) as conexao:
            garantir_schema(conexao, url)
            modelo = classificar_descricao(conexao, descricao, ncm_atual, gerar=gerar)
    except SemChave:
        return 503, {"erro": "A classificação por IA está indisponível."}
    except Exception as exc:
        return 503, {"erro": str(exc)}
    if modelo is None:
        return 422, {"erro": "A sugestão não existe na tabela oficial."}
    codigo = normalizar_ean(ean) if ean else None
    publicado = None
    if codigo:
        status, corpo = consultar_ean(codigo, url)
        if status == 200:
            publicado = corpo
            if (
                not modelo.duvida
                and corpo.get("ncm") == modelo.ncm
                and corpo.get("cclass_trib") == modelo.cclass
            ):
                return 200, {"publicado": corpo, "sugestao": None}
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        produto_id = None
        if codigo:
            dono = conexao.execute(
                "SELECT id FROM produtos WHERE ean = %s",
                (codigo,),
            ).fetchone()
            if dono is not None:
                produto_id = dono["id"]
        salvar_sugestao(
            conexao,
            produto_id=produto_id,
            ean=codigo,
            descricao=descricao.strip(),
            descricao_norm=normalizar_texto(descricao),
            ncm_atual=codigo_ncm(ncm_atual) if ncm_atual else None,
            ncm_sugerido=modelo.ncm,
            cclass_sugerido=modelo.cclass,
            analise=modelo.analise,
            duvida=modelo.duvida,
        )
        conexao.commit()
    return 200, {
        "publicado": publicado,
        "sugestao": {
            "ncm": modelo.ncm,
            "cclass_trib": modelo.cclass,
            "analise": modelo.analise,
            "duvida": modelo.duvida,
        },
    }


def sugerir_lote(itens: list, gerar=None, url: str | None = None):
    from ajuste_fiscal.banco import consultar_lote
    from ajuste_fiscal.normalizar import normalizar_ean

    if not isinstance(itens, list) or not itens:
        return 400, {"erro": "Informe ao menos um produto."}
    if len(itens) > LIMITE_SUGESTAO:
        return 400, {"erro": f"O lote de classificação aceita até {LIMITE_SUGESTAO} produtos."}
    if any(not str(item.get("descricao") or "").strip() for item in itens if isinstance(item, dict)):
        return 400, {"erro": "Informe a descrição de cada produto."}
    if any(not isinstance(item, dict) for item in itens):
        return 400, {"erro": "Informe a descrição de cada produto."}
    if gerar is None and not chave_gemini():
        return 503, {"erro": "A classificação por IA está indisponível."}
    try:
        with conectar(url) as conexao:
            garantir_schema(conexao, url)
            modelos = classificar_lote(
                conexao,
                [(str(item.get("descricao") or ""), item.get("ncm_atual")) for item in itens],
                gerar=gerar,
            )
    except SemChave:
        return 503, {"erro": "A classificação por IA está indisponível."}
    except Exception as exc:
        return 503, {"erro": str(exc)}
    codigos = []
    for item in itens:
        codigo = normalizar_ean(item.get("ean")) if item.get("ean") else None
        codigos.append(codigo)
    publicados = {}
    if any(codigos):
        try:
            lote = consultar_lote([codigo for codigo in codigos if codigo], url=url)
        except Exception:
            lote = {"itens": []}
        publicados = {perfil["ean"]: perfil for perfil in lote["itens"] if perfil.get("ean")}
    respostas = []
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        for item, modelo, codigo in zip(itens, modelos, codigos):
            descricao = str(item.get("descricao") or "").strip()
            publicado = publicados.get(codigo) if codigo else None
            if modelo is None:
                respostas.append(
                    {
                        "descricao": descricao,
                        "ean": codigo,
                        "publicado": publicado,
                        "sugestao": None,
                        "erro": "A sugestão não existe na tabela oficial.",
                    }
                )
                continue
            if (
                publicado
                and not modelo.duvida
                and publicado.get("ncm") == modelo.ncm
                and publicado.get("cclass_trib") == modelo.cclass
            ):
                respostas.append(
                    {"descricao": descricao, "ean": codigo, "publicado": publicado, "sugestao": None}
                )
                continue
            produto_id = None
            if codigo:
                dono = conexao.execute("SELECT id FROM produtos WHERE ean = %s", (codigo,)).fetchone()
                if dono is not None:
                    produto_id = dono["id"]
            salvar_sugestao(
                conexao,
                produto_id=produto_id,
                ean=codigo,
                descricao=descricao,
                descricao_norm=normalizar_texto(descricao),
                ncm_atual=codigo_ncm(item.get("ncm_atual")) if item.get("ncm_atual") else None,
                ncm_sugerido=modelo.ncm,
                cclass_sugerido=modelo.cclass,
                analise=modelo.analise,
                duvida=modelo.duvida,
            )
            respostas.append(
                {
                    "descricao": descricao,
                    "ean": codigo,
                    "publicado": publicado,
                    "sugestao": {
                        "ncm": modelo.ncm,
                        "cclass_trib": modelo.cclass,
                        "analise": modelo.analise,
                        "duvida": modelo.duvida,
                    },
                }
            )
        conexao.commit()
    return 200, {"itens": respostas}


def classificar_lote(conexao, itens, gerar=None) -> list:
    if not itens:
        return []
    if len(itens) == 1:
        return [classificar_descricao(conexao, itens[0][0], itens[0][1], gerar=gerar)]
    classes = conexao.execute(
        "SELECT codigo, descricao, texto_curto FROM cclass_oficial ORDER BY codigo"
    ).fetchall()
    candidatos = []
    exemplos = []
    vistos_ncm = set()
    vistos_exemplo = set()
    for descricao, _ncm_atual in itens:
        achados = _candidatos(conexao, descricao)
        for codigo, texto in achados:
            if codigo not in vistos_ncm:
                vistos_ncm.add(codigo)
                candidatos.append((codigo, texto))
        capitulos = sorted({codigo[:4] for codigo, _texto in achados})
        for exemplo in _exemplos(conexao, capitulos):
            if exemplo not in vistos_exemplo:
                vistos_exemplo.add(exemplo)
                exemplos.append(exemplo)
    pedido = _pedido_lote(itens, candidatos[:120], exemplos[:16], classes)
    texto = (gerar or gerar_gemini)(pedido)
    return _interpretar_lote(conexao, texto, len(itens))


def classificar_descricao(conexao, descricao: str, ncm_atual, gerar=None) -> SugestaoModelo | None:
    candidatos = _candidatos(conexao, descricao)
    capitulos = sorted({codigo[:4] for codigo, _descricao in candidatos})
    exemplos = _exemplos(conexao, capitulos)
    classes = conexao.execute(
        "SELECT codigo, descricao, texto_curto FROM cclass_oficial ORDER BY codigo"
    ).fetchall()
    pedido = _pedido(descricao, ncm_atual, candidatos, exemplos, classes)
    texto = (gerar or gerar_gemini)(pedido)
    return _interpretar(conexao, texto)


def gerar_gemini(pedido: str) -> str:
    chave = chave_gemini()
    if not chave:
        raise SemChave("GEMINI_API_KEY não definida")
    modelo = modelo_gemini()
    endereco = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    corpo = json.dumps(
        {
            "contents": [{"parts": [{"text": pedido}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
    ).encode("utf-8")
    requisicao = Request(
        endereco,
        data=corpo,
        headers={"Content-Type": "application/json", "x-goog-api-key": chave},
        method="POST",
    )
    with urlopen(requisicao, timeout=60) as resposta:
        payload = json.loads(resposta.read().decode("utf-8"))
    partes = payload["candidates"][0]["content"]["parts"]
    return partes[0]["text"]


def _interpretar(conexao, texto: str) -> SugestaoModelo | None:
    try:
        dados = json.loads(_limpar_json(texto))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(dados, dict):
        return None
    return _modelo_de(conexao, dados)


def _interpretar_lote(conexao, texto: str, quantidade: int) -> list:
    try:
        dados = json.loads(_limpar_json(texto))
    except (json.JSONDecodeError, TypeError):
        return [None] * quantidade
    if isinstance(dados, dict) and isinstance(dados.get("itens"), list):
        dados = dados["itens"]
    if isinstance(dados, dict):
        dados = [dados] if quantidade == 1 else []
    if not isinstance(dados, list):
        return [None] * quantidade
    por_indice = {}
    posicionais = []
    for item in dados:
        if not isinstance(item, dict):
            continue
        indice = item.get("indice")
        if isinstance(indice, int) or (isinstance(indice, str) and indice.isdigit()):
            por_indice[int(indice)] = item
        else:
            posicionais.append(item)
    if not por_indice and len(posicionais) == quantidade:
        por_indice = {indice: item for indice, item in enumerate(posicionais)}
    return [_modelo_de(conexao, por_indice[indice]) if indice in por_indice else None for indice in range(quantidade)]


def _modelo_de(conexao, dados: dict) -> SugestaoModelo | None:
    ncm = codigo_ncm(dados.get("ncm"))
    cclass = codigo_cclass(dados.get("cclass_trib") or dados.get("cclass"))
    if ncm is None or cclass is None:
        return None
    ncm_ok = conexao.execute("SELECT 1 FROM ncm_oficial WHERE codigo = %s", (ncm,)).fetchone()
    classe_ok = conexao.execute(
        "SELECT 1 FROM cclass_oficial WHERE codigo = %s",
        (cclass,),
    ).fetchone()
    if ncm_ok is None or classe_ok is None:
        return None
    analise = str(dados.get("analise") or "").strip()
    return SugestaoModelo(ncm=ncm, cclass=cclass, analise=analise, duvida=bool(dados.get("duvida")))


def _limpar_json(texto: str) -> str:
    limpo = (texto or "").strip()
    if limpo.startswith("```"):
        limpo = re.sub(r"^```(?:json)?", "", limpo, flags=re.IGNORECASE).strip()
        limpo = re.sub(r"```$", "", limpo).strip()
    return limpo


def _candidatos(conexao, descricao: str) -> list[tuple[str, str]]:
    palavras = [
        token
        for token in normalizar_texto(descricao).split()
        if len(token) >= 4 and token not in _PARADA and not token.isdigit()
    ]
    vistos = []
    for palavra in palavras[:6]:
        linhas = conexao.execute(
            """
            SELECT codigo, descricao FROM ncm_oficial
            WHERE descricao ILIKE %s
            ORDER BY codigo
            LIMIT 20
            """,
            (f"%{palavra}%",),
        ).fetchall()
        vistos.extend((linha["codigo"], linha["descricao"]) for linha in linhas)
    if not vistos and palavras:
        return []
    capitulos = {}
    for codigo, _descricao in vistos:
        capitulos[codigo[:4]] = capitulos.get(codigo[:4], 0) + 1
    escolhidos = [codigo for codigo, _quantidade in sorted(capitulos.items(), key=lambda item: (-item[1], item[0]))[:2]]
    if not escolhidos:
        return vistos[:40]
    linhas = []
    for capitulo in escolhidos:
        achados = conexao.execute(
            """
            SELECT codigo, descricao FROM ncm_oficial
            WHERE codigo LIKE %s
            ORDER BY codigo
            LIMIT 40
            """,
            (f"{capitulo}%",),
        ).fetchall()
        linhas.extend((linha["codigo"], linha["descricao"]) for linha in achados)
    return linhas[:80]


def _exemplos(conexao, capitulos: list[str]) -> list[tuple[str, str, str]]:
    exemplos = []
    for capitulo in capitulos:
        linhas = conexao.execute(
            """
            SELECT descricao, ncm, cclass_trib FROM produtos
            WHERE ncm LIKE %s AND ncm IS NOT NULL AND cclass_trib IS NOT NULL
            ORDER BY id
            LIMIT 8
            """,
            (f"{capitulo}%",),
        ).fetchall()
        exemplos.extend(
            (linha["descricao"], linha["ncm"], linha["cclass_trib"]) for linha in linhas
        )
    return exemplos[:8]


def _pedido(descricao, ncm_atual, candidatos, exemplos, classes) -> str:
    lista_ncm = "\n".join(f"{codigo} {texto}" for codigo, texto in candidatos[:80]) or "(nenhum candidato)"
    lista_exemplos = "\n".join(
        f"{texto} | {ncm} | {cclass}" for texto, ncm, cclass in exemplos
    ) or "(nenhum produto confirmado neste capítulo)"
    lista_classes = "\n".join(
        f"{linha['codigo']} {linha['texto_curto'] or linha['descricao'] or ''}".strip()
        for linha in classes
    ) or "(nenhuma classificação oficial carregada)"
    return (
        "Classifique o produto na NCM e no cClassTrib do IBS/CBS. "
        "Responda apenas JSON com as chaves ncm, cclass_trib, analise e duvida. "
        "ncm tem 8 dígitos e cclass_trib tem 6, ambos copiados das listas oficiais abaixo. "
        "duvida é true quando a descrição não basta para decidir, por exemplo composição, uso ou apresentação. "
        "analise explica a escolha em uma frase, com o dispositivo ou a regra usada quando souber.\n"
        f"Descrição: {descricao}\n"
        f"NCM atual: {ncm_atual or 'não informado'}\n"
        f"cClassTrib oficiais:\n{lista_classes}\n"
        f"NCMs oficiais candidatos:\n{lista_ncm}\n"
        f"Produtos já confirmados:\n{lista_exemplos}\n"
    )


def _pedido_lote(itens, candidatos, exemplos, classes) -> str:
    lista_ncm = "\n".join(f"{codigo} {texto}" for codigo, texto in candidatos) or "(nenhum candidato)"
    lista_exemplos = "\n".join(
        f"{texto} | {ncm} | {cclass}" for texto, ncm, cclass in exemplos
    ) or "(nenhum produto confirmado neste capítulo)"
    lista_classes = "\n".join(
        f"{linha['codigo']} {linha['texto_curto'] or linha['descricao'] or ''}".strip()
        for linha in classes
    ) or "(nenhuma classificação oficial carregada)"
    produtos = "\n".join(
        f"{indice} | {descricao} | {ncm_atual or 'não informado'}"
        for indice, (descricao, ncm_atual) in enumerate(itens)
    )
    return (
        "Classifique cada produto na NCM e no cClassTrib do IBS/CBS. "
        "Responda apenas um JSON array, um objeto por produto, na mesma ordem. "
        "Cada objeto tem indice, ncm, cclass_trib, analise e duvida. "
        "ncm tem 8 dígitos e cclass_trib tem 6, ambos copiados das listas oficiais. "
        "duvida é true quando a descrição não basta para decidir, por exemplo composição, uso ou apresentação. "
        "analise é uma frase curta.\n"
        f"cClassTrib oficiais:\n{lista_classes}\n"
        f"NCMs oficiais candidatos:\n{lista_ncm}\n"
        f"Produtos já confirmados:\n{lista_exemplos}\n"
        f"Produtos:\n{produtos}\n"
    )
