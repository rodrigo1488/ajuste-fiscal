"""Base PostgreSQL dos produtos com classificação fiscal confirmada."""

import hashlib
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from ajuste_fiscal.aplicar import Resultado
from ajuste_fiscal.contabilidade import ItemContabilidade
from ajuste_fiscal.normalizar import (
    codigo_cest,
    codigo_cfop,
    codigo_fiscal,
    normalizar_ean,
    normalizar_texto,
    numero_fiscal,
    partir_natureza,
)
from ajuste_fiscal.sistema import PlanilhaSistema

URL_PADRAO = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal"
_PRONTOS: set[str] = set()
_TRAVA = threading.Lock()

_COMANDOS = (
    """
    CREATE TABLE IF NOT EXISTS produtos (
        id bigserial PRIMARY KEY,
        descricao_norm text NOT NULL UNIQUE,
        descricao text NOT NULL,
        ean text,
        ncm text,
        ncm_anterior text,
        cest text,
        cst_ibs_cbs text,
        cclass_trib text,
        descricao_cclass text,
        reducao_cbs numeric,
        reducao_ibs_uf numeric,
        reducao_ibs_mun numeric,
        imposto_seletivo text,
        natureza_codigo text,
        natureza_descricao text,
        cfop_pdv text,
        cfop_nfe text,
        cst_icms text,
        csosn text,
        cst_pis text,
        aliq_pis numeric,
        cst_cofins text,
        aliq_cofins numeric,
        cst_ipi text,
        aliq_ipi numeric,
        situacao text NOT NULL,
        coletas integer NOT NULL DEFAULT 1,
        embasamento_classificacao text,
        embasamento_legal text,
        referencia_normativa text,
        tabela_em date,
        atualizado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE produtos ADD COLUMN IF NOT EXISTS embasamento_classificacao text",
    "ALTER TABLE produtos ADD COLUMN IF NOT EXISTS embasamento_legal text",
    "ALTER TABLE produtos ADD COLUMN IF NOT EXISTS referencia_normativa text",
    "ALTER TABLE produtos ADD COLUMN IF NOT EXISTS tabela_em date",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS produtos_ean_unico
        ON produtos (ean) WHERE ean IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS coletas (
        id bigserial PRIMARY KEY,
        produto_id bigint REFERENCES produtos (id),
        descricao_norm text NOT NULL,
        arquivo text,
        ean text,
        ncm text,
        cclass_trib text,
        natureza_codigo text,
        natureza_descricao text,
        cfop_pdv text,
        cfop_nfe text,
        situacao text,
        criado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflitos (
        id bigserial PRIMARY KEY,
        produto_id bigint REFERENCES produtos (id),
        descricao_norm text NOT NULL,
        ean text,
        campo text NOT NULL,
        valor_publicado text,
        valor_recebido text,
        arquivo text,
        criado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vigencia_oficial (
        chave text PRIMARY KEY,
        data date NOT NULL,
        atualizado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ncm_oficial (
        codigo text PRIMARY KEY,
        descricao text NOT NULL,
        vigencia date NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cclass_oficial (
        codigo text PRIMARY KEY,
        descricao text,
        cst text,
        texto text,
        texto_curto text,
        referencia_normativa text,
        possui_reducao boolean,
        reducao_cbs numeric,
        reducao_ibs_uf numeric,
        reducao_ibs_mun numeric,
        vigencia date NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS aliquota_oficial (
        tributo text PRIMARY KEY,
        aliquota numeric,
        vigencia date NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seletivo_oficial (
        ncm text PRIMARY KEY,
        tributado boolean NOT NULL,
        aliquota_ad_valorem numeric,
        aliquota_ad_rem numeric,
        vigencia date NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sugestoes (
        id bigserial PRIMARY KEY,
        produto_id bigint REFERENCES produtos (id),
        ean text,
        descricao text NOT NULL,
        descricao_norm text NOT NULL,
        ncm_atual text,
        ncm_sugerido text,
        cclass_sugerido text,
        analise text,
        duvida boolean NOT NULL DEFAULT false,
        situacao text NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parceiros (
        id bigserial PRIMARY KEY,
        nome text NOT NULL,
        cnpj text NOT NULL UNIQUE,
        token_hash text NOT NULL UNIQUE,
        ativo boolean NOT NULL DEFAULT true,
        criado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regras_fiscais (
        id bigserial PRIMARY KEY,
        vigencia_ini date NOT NULL,
        vigencia_fim date,
        uf text NOT NULL DEFAULT '',
        regime text NOT NULL DEFAULT '',
        ncm text NOT NULL DEFAULT '',
        cest text NOT NULL DEFAULT '',
        exige_cest boolean NOT NULL DEFAULT false,
        tipo text NOT NULL,
        cfop text,
        cst text,
        csosn text,
        aliq_pis numeric,
        aliq_cofins numeric,
        aliq_ipi numeric,
        aliq_icms numeric,
        mva numeric,
        reducao numeric,
        amparo_legal text NOT NULL,
        UNIQUE (tipo, uf, regime, ncm, cest, exige_cest, vigencia_ini)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS configuracao (
        chave text PRIMARY KEY,
        valor text NOT NULL,
        atualizado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS saneamentos (
        id bigserial PRIMARY KEY,
        arquivo text,
        lidos integer NOT NULL,
        sanitizados integer NOT NULL,
        conformes integer NOT NULL,
        sem_par integer NOT NULL,
        conflitos integer NOT NULL,
        pendentes integer NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cadastros (
        id bigserial PRIMARY KEY,
        saneamento_id bigint NOT NULL REFERENCES saneamentos (id),
        identificador text,
        descricao text NOT NULL,
        ean text,
        ncm_original text,
        ncm text,
        cest_original text,
        cest text,
        cclass_original text,
        cclass text,
        situacao text NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS cadastros_saneamento_idx
        ON cadastros (saneamento_id, situacao, id)
    """,
)

_CAMPOS_VAZIOS = (
    "ean",
    "ncm_anterior",
    "cest",
    "cst_ibs_cbs",
    "descricao_cclass",
    "reducao_cbs",
    "reducao_ibs_uf",
    "reducao_ibs_mun",
    "imposto_seletivo",
    "natureza_codigo",
    "natureza_descricao",
    "cfop_pdv",
    "cfop_nfe",
    "cst_icms",
    "csosn",
    "cst_pis",
    "aliq_pis",
    "cst_cofins",
    "aliq_cofins",
    "cst_ipi",
    "aliq_ipi",
    "embasamento_classificacao",
)


@dataclass
class ResumoColeta:
    inseridos: int = 0
    atualizados: int = 0
    conflitos: int = 0
    ignorados: int = 0
    erro: str | None = None

    def como_dict(self) -> dict:
        return {
            "inseridos": self.inseridos,
            "atualizados": self.atualizados,
            "conflitos": self.conflitos,
            "ignorados": self.ignorados,
            "erro": self.erro,
        }


def url_banco() -> str:
    return os.environ.get("DATABASE_URL", URL_PADRAO)


def registrar(resultado: Resultado, nome_arquivo: str = "", url: str | None = None) -> ResumoColeta:
    endereco = url or url_banco()
    resumo = ResumoColeta()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        linhas = {linha.numero: linha for linha in resultado.planilha.linhas}
        casados = {alteracao.item.linha: alteracao for alteracao in resultado.aplicados}
        for item in resultado.itens:
            if not item.definitivo or not item.descricao_norm or (item.ncm is None and item.cclass is None):
                resumo.ignorados += 1
                continue
            observado = {}
            alteracao = casados.get(item.linha)
            if alteracao is not None:
                observado = _observado(resultado.planilha, linhas.get(alteracao.numero_sistema))
            perfil = _perfil(item, observado)
            estado = _gravar_um(conexao, perfil, nome_arquivo)
            if estado == "conflito":
                resumo.conflitos += 1
            elif estado == "novo":
                resumo.inseridos += 1
            else:
                resumo.atualizados += 1
        conexao.commit()
    return resumo


def consultar_ean(ean: str, url: str | None = None) -> tuple[int, dict]:
    codigo = normalizar_ean(ean)
    if codigo is None:
        return 404, {"erro": "EAN sem cadastro na base", "ean": str(ean or "")}
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        produto = conexao.execute(
            "SELECT * FROM produtos WHERE ean = %s",
            (codigo,),
        ).fetchone()
        if produto is None:
            return 404, {"erro": "EAN sem cadastro na base", "ean": codigo}
        aberto = conexao.execute(
            """
            SELECT 1 FROM conflitos
            WHERE produto_id = %s OR ean = %s
            LIMIT 1
            """,
            (produto["id"], codigo),
        ).fetchone()
        if aberto is not None:
            return 409, {
                "erro": "EAN com classificação divergente ainda não resolvida",
                "ean": codigo,
            }
        corpo = _publico(produto)
        corpo["sugestao_pendente"] = _sugestao_publica(
            conexao.execute(
                """
                SELECT ncm_sugerido, cclass_sugerido, analise, duvida
                FROM sugestoes
                WHERE situacao = 'pendente' AND (produto_id = %s OR ean = %s)
                ORDER BY id DESC
                LIMIT 1
                """,
                (produto["id"], codigo),
            ).fetchone()
        )
    return 200, corpo


LIMITE_LOTE = 100


def consultar_lote(eans=None, descricoes=None, completo: bool = False, url: str | None = None) -> dict:
    """Consulta vários produtos de uma vez e omite o texto longo quando completo é falso."""
    pedidos_ean = list(eans or [])
    pedidos_descricao = list(descricoes or [])
    if not pedidos_ean and not pedidos_descricao:
        raise ValueError("Informe ao menos um EAN ou uma descrição.")
    if len(pedidos_ean) + len(pedidos_descricao) > LIMITE_LOTE:
        raise ValueError(f"O lote aceita até {LIMITE_LOTE} consultas.")
    invalidos = []
    pedidos = []
    for bruto in pedidos_ean:
        codigo = normalizar_ean(str(bruto))
        if codigo is None:
            invalidos.append(str(bruto))
        else:
            pedidos.append(("ean", codigo, codigo))
    for bruto in pedidos_descricao:
        texto = str(bruto or "").strip()
        if not texto:
            invalidos.append(str(bruto))
            continue
        pedidos.append(("descricao", texto, normalizar_texto(texto)))
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        codigos = [chave for tipo, _rotulo, chave in pedidos if tipo == "ean"]
        normas = [chave for tipo, _rotulo, chave in pedidos if tipo == "descricao"]
        por_ean = {
            linha["ean"]: linha
            for linha in (
                conexao.execute("SELECT * FROM produtos WHERE ean = ANY(%s)", (codigos,)).fetchall()
                if codigos
                else []
            )
        }
        por_descricao = {
            linha["descricao_norm"]: linha
            for linha in (
                conexao.execute(
                    "SELECT * FROM produtos WHERE descricao_norm = ANY(%s)",
                    (normas,),
                ).fetchall()
                if normas
                else []
            )
        }
        produtos = list(por_ean.values()) + list(por_descricao.values())
        ids = list({linha["id"] for linha in produtos})
        conflitos_id, conflitos_ean = _conflitos_abertos(conexao, ids, codigos)
        sugestoes = _sugestoes_por_produto(conexao, ids, codigos) if completo or ids else {}
    itens = []
    ausentes = []
    conflitos = []
    vistos_pedido = set()
    vistos_produto = set()
    for tipo, rotulo, chave in pedidos:
        if (tipo, chave) in vistos_pedido:
            continue
        vistos_pedido.add((tipo, chave))
        produto = por_ean.get(chave) if tipo == "ean" else por_descricao.get(chave)
        if produto is None:
            ausentes.append(rotulo)
            continue
        if produto["id"] in vistos_produto:
            continue
        vistos_produto.add(produto["id"])
        if produto["id"] in conflitos_id or (produto["ean"] and produto["ean"] in conflitos_ean):
            conflitos.append(rotulo)
            continue
        if completo:
            item = _publico(produto)
            item["sugestao_pendente"] = sugestoes.get(produto["id"])
        else:
            item = _compacto(produto)
            if produto["id"] in sugestoes:
                item["pendente"] = True
        itens.append(item)
    return {"itens": itens, "ausentes": ausentes, "invalidos": invalidos, "conflitos": conflitos}


def conectar(url: str | None = None):
    return psycopg.connect(url or url_banco(), row_factory=dict_row)


def garantir_schema(conexao, url: str | None = None) -> None:
    chave = url or url_banco()
    with _TRAVA:
        if chave in _PRONTOS:
            return
        for comando in _COMANDOS:
            conexao.execute(comando)
        _semear_regras(conexao)
        conexao.commit()
        _PRONTOS.add(chave)


def _semear_regras(conexao) -> None:
    vigencia = date(2026, 1, 1)
    linhas = []
    linhas.append(_regra(vigencia, "icms", uf="SP", exige_cest=True, cfop="5405", cst="60", csosn="500", aliq_icms=0, amparo="Convênio ICMS 142/2018; saída interna com substituição tributária já retida"))
    linhas.append(_regra(vigencia, "icms", uf="SP", cfop="5102", cst="00", csosn="102", aliq_icms=18, amparo="RICMS/SP, art. 52, inciso I"))
    for regime in ("SN", "LP"):
        linhas.append(_regra(vigencia, "pis_cofins", regime=regime, cst="01", aliq_pis=Decimal("0.65"), aliq_cofins=Decimal("3"), amparo="Lei 9.715/1998 e Lei 9.718/1998"))
    linhas.append(_regra(vigencia, "pis_cofins", regime="LR", cst="01", aliq_pis=Decimal("1.65"), aliq_cofins=Decimal("7.6"), amparo="Lei 10.637/2002 e Lei 10.833/2003"))
    for prefixo, amparo in (
        ("2201", "Lei 13.097/2015, regime monofásico de bebidas"),
        ("2202", "Lei 13.097/2015, regime monofásico de bebidas"),
        ("2203", "Lei 13.097/2015, regime monofásico de bebidas"),
        ("3003", "Lei 10.147/2000"),
        ("3004", "Lei 10.147/2000"),
        ("8708", "Lei 10.485/2002"),
    ):
        linhas.append(_regra(vigencia, "pis_cofins", ncm=prefixo, cst="04", aliq_pis=0, aliq_cofins=0, amparo=amparo))
    linhas.append(_regra(vigencia, "ipi", cst="53", aliq_ipi=0, amparo="RIPI, Decreto 7.212/2010; estabelecimento comercial não contribuinte do IPI"))
    conexao.cursor().executemany(
        """
        INSERT INTO regras_fiscais (
            vigencia_ini, uf, regime, ncm, cest, exige_cest, tipo, cfop, cst, csosn,
            aliq_pis, aliq_cofins, aliq_ipi, aliq_icms, amparo_legal
        ) VALUES (
            %(vigencia_ini)s, %(uf)s, %(regime)s, %(ncm)s, %(cest)s, %(exige_cest)s, %(tipo)s,
            %(cfop)s, %(cst)s, %(csosn)s, %(aliq_pis)s, %(aliq_cofins)s, %(aliq_ipi)s,
            %(aliq_icms)s, %(amparo_legal)s
        )
        ON CONFLICT (tipo, uf, regime, ncm, cest, exige_cest, vigencia_ini) DO NOTHING
        """,
        linhas,
    )


def _regra(vigencia, tipo, amparo, uf="", regime="", ncm="", cest="", exige_cest=False, cfop=None, cst=None, csosn=None, aliq_pis=None, aliq_cofins=None, aliq_ipi=None, aliq_icms=None) -> dict:
    return {
        "vigencia_ini": vigencia,
        "uf": uf,
        "regime": regime,
        "ncm": ncm,
        "cest": cest,
        "exige_cest": exige_cest,
        "tipo": tipo,
        "cfop": cfop,
        "cst": cst,
        "csosn": csosn,
        "aliq_pis": aliq_pis,
        "aliq_cofins": aliq_cofins,
        "aliq_ipi": aliq_ipi,
        "aliq_icms": aliq_icms,
        "amparo_legal": amparo,
    }


def criar_parceiro(nome: str, cnpj: str, url: str | None = None) -> dict:
    documento = "".join(caractere for caractere in str(cnpj or "") if caractere.isdigit())
    titulo = str(nome or "").strip()
    if len(documento) != 14 or not titulo:
        raise ValueError("Informe o nome e o CNPJ do parceiro.")
    token = secrets.token_urlsafe(32)
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        try:
            conexao.execute(
                "INSERT INTO parceiros (nome, cnpj, token_hash) VALUES (%s, %s, %s)",
                (titulo, documento, hashlib.sha256(token.encode()).hexdigest()),
            )
            conexao.commit()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "23505":
                raise
            raise ValueError("Já existe um parceiro com este CNPJ.") from exc
    return {"nome": titulo, "cnpj": documento, "token": token}


def parceiro_por_token(token: str, url: str | None = None):
    segredo = str(token or "").strip()
    if not segredo:
        return None
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        return conexao.execute(
            "SELECT id, nome, cnpj FROM parceiros WHERE token_hash = %s AND ativo",
            (hashlib.sha256(segredo.encode()).hexdigest(),),
        ).fetchone()


def ler_configuracao(chave: str, url: str | None = None) -> str | None:
    endereco = url or url_banco()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        linha = conexao.execute(
            "SELECT valor FROM configuracao WHERE chave = %s",
            (chave,),
        ).fetchone()
    return linha["valor"] if linha else None


def gravar_configuracao(chave: str, valor: str, url: str | None = None) -> None:
    endereco = url or url_banco()
    texto = str(valor or "").strip()
    with conectar(endereco) as conexao:
        garantir_schema(conexao, endereco)
        if texto:
            conexao.execute(
                """
                INSERT INTO configuracao (chave, valor, atualizado_em)
                VALUES (%s, %s, now())
                ON CONFLICT (chave) DO UPDATE
                SET valor = EXCLUDED.valor, atualizado_em = now()
                """,
                (chave, texto),
            )
        else:
            conexao.execute("DELETE FROM configuracao WHERE chave = %s", (chave,))
        conexao.commit()


def limpar(url: str) -> None:
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        conexao.execute(
            "TRUNCATE cadastros, saneamentos, sugestoes, conflitos, coletas, produtos RESTART IDENTITY"
        )
        conexao.commit()


def _gravar_um(conexao, perfil: dict, arquivo: str) -> str:
    """Grava um perfil. Devolve True quando a classificação divergiu."""
    existente = conexao.execute(
        "SELECT * FROM produtos WHERE descricao_norm = %s",
        (perfil["descricao_norm"],),
    ).fetchone()
    if existente is None and perfil["ean"]:
        por_ean = conexao.execute(
            "SELECT * FROM produtos WHERE ean = %s",
            (perfil["ean"],),
        ).fetchone()
        if por_ean is not None and por_ean["descricao_norm"] != perfil["descricao_norm"]:
            _anotar_descricao_nova(conexao, por_ean, perfil, arquivo)
            return "atualizado"
        if por_ean is not None:
            existente = por_ean
    if existente is None:
        _inserir(conexao, perfil, arquivo)
        return "novo"
    divergencias = _divergencias(existente, perfil)
    if divergencias:
        for campo, publicado, recebido in divergencias:
            conexao.execute(
                """
                INSERT INTO conflitos (
                    produto_id, descricao_norm, ean, campo, valor_publicado, valor_recebido, arquivo
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    existente["id"],
                    perfil["descricao_norm"],
                    perfil["ean"] or existente["ean"],
                    campo,
                    publicado,
                    recebido,
                    arquivo or None,
                ),
            )
        _registrar_coleta(conexao, existente["id"], perfil, arquivo)
        conexao.execute(
            "UPDATE produtos SET coletas = coletas + 1, atualizado_em = %s WHERE id = %s",
            (datetime.now(timezone.utc), existente["id"]),
        )
        return "conflito"
    _completar(conexao, existente, perfil)
    _registrar_coleta(conexao, existente["id"], perfil, arquivo)
    return "atualizado"


def _inserir(conexao, perfil: dict, arquivo: str) -> None:
    if perfil["ean"]:
        dono = conexao.execute(
            "SELECT id FROM produtos WHERE ean = %s",
            (perfil["ean"],),
        ).fetchone()
        if dono is not None:
            perfil = {**perfil, "ean": None}
    colunas = [
        "descricao_norm",
        "descricao",
        "ean",
        "ncm",
        "ncm_anterior",
        "cest",
        "cst_ibs_cbs",
        "cclass_trib",
        "descricao_cclass",
        "reducao_cbs",
        "reducao_ibs_uf",
        "reducao_ibs_mun",
        "imposto_seletivo",
        "natureza_codigo",
        "natureza_descricao",
        "cfop_pdv",
        "cfop_nfe",
        "cst_icms",
        "csosn",
        "cst_pis",
        "aliq_pis",
        "cst_cofins",
        "aliq_cofins",
        "cst_ipi",
        "aliq_ipi",
        "situacao",
        "embasamento_classificacao",
    ]
    marcas = ", ".join(["%s"] * len(colunas))
    conexao.execute(
        f"INSERT INTO produtos ({', '.join(colunas)}) VALUES ({marcas}) RETURNING id",
        tuple(perfil[coluna] for coluna in colunas),
    )
    produto_id = conexao.execute(
        "SELECT id FROM produtos WHERE descricao_norm = %s",
        (perfil["descricao_norm"],),
    ).fetchone()["id"]
    _registrar_coleta(conexao, produto_id, perfil, arquivo)


def _completar(conexao, existente: dict, perfil: dict) -> None:
    valores = {}
    for campo in _CAMPOS_VAZIOS:
        if not _preenchido(existente.get(campo)) and _preenchido(perfil.get(campo)):
            if campo == "ean":
                outro = conexao.execute(
                    "SELECT id FROM produtos WHERE ean = %s AND id <> %s",
                    (perfil["ean"], existente["id"]),
                ).fetchone()
                if outro is not None:
                    continue
            valores[campo] = perfil[campo]
    atribuicoes = ", ".join(f"{campo} = %s" for campo in valores)
    parametros = list(valores.values())
    parametros.extend([datetime.now(timezone.utc), existente["id"]])
    trecho = f"{atribuicoes}, " if atribuicoes else ""
    conexao.execute(
        f"UPDATE produtos SET {trecho}coletas = coletas + 1, atualizado_em = %s WHERE id = %s",
        parametros,
    )


def _divergencias(existente: dict, perfil: dict) -> list[tuple[str, str, str]]:
    achados = []
    for campo in ("ncm", "cclass_trib"):
        publicado = existente.get(campo)
        recebido = perfil.get(campo)
        if _preenchido(publicado) and _preenchido(recebido) and str(publicado) != str(recebido):
            achados.append((campo, str(publicado), str(recebido)))
    return achados


def _registrar_coleta(conexao, produto_id: int, perfil: dict, arquivo: str) -> None:
    conexao.execute(
        """
        INSERT INTO coletas (
            produto_id, descricao_norm, arquivo, ean, ncm, cclass_trib,
            natureza_codigo, natureza_descricao, cfop_pdv, cfop_nfe, situacao
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            produto_id,
            perfil["descricao_norm"],
            arquivo or None,
            perfil["ean"],
            perfil["ncm"],
            perfil["cclass_trib"],
            perfil["natureza_codigo"],
            perfil["natureza_descricao"],
            perfil["cfop_pdv"],
            perfil["cfop_nfe"],
            perfil["situacao"],
        ),
    )


def _perfil(item: ItemContabilidade, observado: dict) -> dict:
    situacao = "corrigido" if normalizar_texto(item.status) == "INCORRETO" else "correto"
    perfil = {
        "descricao_norm": item.descricao_norm,
        "descricao": item.descricao,
        "ean": observado.get("ean"),
        "ncm": item.ncm,
        "ncm_anterior": item.ncm_anterior if situacao == "corrigido" else None,
        "cest": item.cest or observado.get("cest"),
        "cst_ibs_cbs": item.cst_ibs_cbs,
        "cclass_trib": item.cclass,
        "descricao_cclass": item.descricao_cclass,
        "reducao_cbs": _decimal(item.reducao_cbs),
        "reducao_ibs_uf": _decimal(item.reducao_ibs_uf),
        "reducao_ibs_mun": _decimal(item.reducao_ibs_mun),
        "imposto_seletivo": item.imposto_seletivo,
        "natureza_codigo": item.natureza_codigo or observado.get("natureza_codigo"),
        "natureza_descricao": item.natureza_descricao or observado.get("natureza_descricao"),
        "cfop_pdv": item.cfop_pdv or observado.get("cfop_pdv"),
        "cfop_nfe": item.cfop_nfe or observado.get("cfop_nfe"),
        "cst_icms": item.cst_icms or observado.get("cst_icms"),
        "csosn": observado.get("csosn"),
        "cst_pis": observado.get("cst_pis"),
        "aliq_pis": _decimal(observado.get("aliq_pis")),
        "cst_cofins": observado.get("cst_cofins"),
        "aliq_cofins": _decimal(observado.get("aliq_cofins")),
        "cst_ipi": observado.get("cst_ipi"),
        "aliq_ipi": _decimal(observado.get("aliq_ipi")),
        "situacao": situacao,
        "embasamento_classificacao": item.analise,
    }
    return perfil


def _observado(planilha: PlanilhaSistema, linha) -> dict:
    if linha is None or not linha.campos:
        return {}
    natureza_codigo, natureza_descricao = partir_natureza(_celula(planilha, linha, "natureza"))
    return {
        "ean": normalizar_ean(_celula(planilha, linha, "ean")),
        "cest": codigo_cest(_celula(planilha, linha, "cest")),
        "cfop_pdv": codigo_cfop(_celula(planilha, linha, "cfop_pdv")),
        "cfop_nfe": codigo_cfop(_celula(planilha, linha, "cfop_nfe")),
        "cst_icms": codigo_fiscal(_celula(planilha, linha, "cst_icms"), 3),
        "csosn": _texto(_celula(planilha, linha, "csosn")),
        "cst_pis": _texto(_celula(planilha, linha, "cst_pis")),
        "aliq_pis": numero_fiscal(_celula(planilha, linha, "aliq_pis")),
        "cst_cofins": _texto(_celula(planilha, linha, "cst_cofins")),
        "aliq_cofins": numero_fiscal(_celula(planilha, linha, "aliq_cofins")),
        "cst_ipi": _texto(_celula(planilha, linha, "cst_ipi")),
        "aliq_ipi": numero_fiscal(_celula(planilha, linha, "aliq_ipi")),
        "natureza_codigo": natureza_codigo,
        "natureza_descricao": natureza_descricao,
    }


def completar_observacao(conexao, produto: dict, observado: dict, arquivo: str = "") -> None:
    """Preenche no cadastro publicado só o que ainda estava vazio. NCM e cClassTrib não mudam."""
    perfil = {
        "descricao_norm": produto["descricao_norm"],
        "descricao": produto["descricao"],
        "ean": produto.get("ean") or observado.get("ean"),
        "ncm": produto.get("ncm"),
        "ncm_anterior": produto.get("ncm_anterior"),
        "cest": produto.get("cest") or observado.get("cest"),
        "cst_ibs_cbs": produto.get("cst_ibs_cbs"),
        "cclass_trib": produto.get("cclass_trib"),
        "descricao_cclass": produto.get("descricao_cclass"),
        "reducao_cbs": produto.get("reducao_cbs"),
        "reducao_ibs_uf": produto.get("reducao_ibs_uf"),
        "reducao_ibs_mun": produto.get("reducao_ibs_mun"),
        "imposto_seletivo": produto.get("imposto_seletivo"),
        "natureza_codigo": produto.get("natureza_codigo") or observado.get("natureza_codigo"),
        "natureza_descricao": produto.get("natureza_descricao") or observado.get("natureza_descricao"),
        "cfop_pdv": produto.get("cfop_pdv") or observado.get("cfop_pdv"),
        "cfop_nfe": produto.get("cfop_nfe") or observado.get("cfop_nfe"),
        "cst_icms": produto.get("cst_icms") or observado.get("cst_icms"),
        "csosn": produto.get("csosn") or observado.get("csosn"),
        "cst_pis": produto.get("cst_pis") or observado.get("cst_pis"),
        "aliq_pis": produto.get("aliq_pis") if _preenchido(produto.get("aliq_pis")) else _decimal(observado.get("aliq_pis")),
        "cst_cofins": produto.get("cst_cofins") or observado.get("cst_cofins"),
        "aliq_cofins": produto.get("aliq_cofins")
        if _preenchido(produto.get("aliq_cofins"))
        else _decimal(observado.get("aliq_cofins")),
        "cst_ipi": produto.get("cst_ipi") or observado.get("cst_ipi"),
        "aliq_ipi": produto.get("aliq_ipi") if _preenchido(produto.get("aliq_ipi")) else _decimal(observado.get("aliq_ipi")),
        "situacao": produto.get("situacao") or "correto",
        "embasamento_classificacao": produto.get("embasamento_classificacao"),
    }
    _gravar_um(conexao, perfil, arquivo)


def _celula(planilha: PlanilhaSistema, linha, chave: str) -> str:
    indice = planilha.indices.get(chave, -1)
    if indice < 0 or indice >= len(linha.campos):
        return ""
    return linha.campos[indice].strip()


def _conflitos_abertos(conexao, ids: list, eans: list) -> tuple[set, set]:
    if not ids and not eans:
        return set(), set()
    linhas = conexao.execute(
        """
        SELECT produto_id, ean FROM conflitos
        WHERE produto_id = ANY(%s) OR ean = ANY(%s)
        """,
        (ids or [0], eans or [""]),
    ).fetchall()
    return {linha["produto_id"] for linha in linhas if linha["produto_id"]}, {
        linha["ean"] for linha in linhas if linha["ean"]
    }


def _sugestoes_por_produto(conexao, ids: list, eans: list) -> dict:
    if not ids and not eans:
        return {}
    linhas = conexao.execute(
        """
        SELECT DISTINCT ON (COALESCE(produto_id::text, ean))
            produto_id, ean, ncm_sugerido, cclass_sugerido, analise, duvida
        FROM sugestoes
        WHERE situacao = 'pendente' AND (produto_id = ANY(%s) OR ean = ANY(%s))
        ORDER BY COALESCE(produto_id::text, ean), id DESC
        """,
        (ids or [0], eans or [""]),
    ).fetchall()
    return {linha["produto_id"]: _sugestao_publica(linha) for linha in linhas if linha["produto_id"]}


def _compacto(produto: dict) -> dict:
    corpo = {
        "ean": produto["ean"],
        "descricao": produto["descricao"],
        "ncm": produto["ncm"],
        "cclass_trib": produto["cclass_trib"],
        "cst_ibs_cbs": produto["cst_ibs_cbs"],
        "cest": produto["cest"],
        "cfop_pdv": produto["cfop_pdv"],
        "cfop_nfe": produto["cfop_nfe"],
        "situacao": produto["situacao"],
        "imposto_seletivo": produto["imposto_seletivo"],
    }
    if produto.get("natureza_codigo"):
        corpo["natureza"] = produto["natureza_codigo"]
    if any(_json_numero(produto.get(campo)) not in (None, 0) for campo in ("reducao_cbs", "reducao_ibs_uf", "reducao_ibs_mun")):
        corpo["reducao_cbs"] = _json_numero(produto["reducao_cbs"])
        corpo["reducao_ibs_uf"] = _json_numero(produto["reducao_ibs_uf"])
        corpo["reducao_ibs_mun"] = _json_numero(produto["reducao_ibs_mun"])
    return {chave: valor for chave, valor in corpo.items() if valor not in (None, "")}


def _publico(produto: dict) -> dict:
    return {
        "ean": produto["ean"],
        "descricao": produto["descricao"],
        "ncm": produto["ncm"],
        "ncm_anterior": produto["ncm_anterior"],
        "cest": produto["cest"],
        "cst_ibs_cbs": produto["cst_ibs_cbs"],
        "cclass_trib": produto["cclass_trib"],
        "descricao_cclass": produto["descricao_cclass"],
        "reducao_cbs": _json_numero(produto["reducao_cbs"]),
        "reducao_ibs_uf": _json_numero(produto["reducao_ibs_uf"]),
        "reducao_ibs_mun": _json_numero(produto["reducao_ibs_mun"]),
        "imposto_seletivo": produto["imposto_seletivo"],
        "natureza_operacao": {
            "codigo": produto["natureza_codigo"],
            "descricao": produto["natureza_descricao"],
        },
        "cfop_pdv": produto["cfop_pdv"],
        "cfop_nfe": produto["cfop_nfe"],
        "cst_icms": produto["cst_icms"],
        "csosn": produto["csosn"],
        "pis": {"cst": produto["cst_pis"], "aliquota": _json_numero(produto["aliq_pis"])},
        "cofins": {"cst": produto["cst_cofins"], "aliquota": _json_numero(produto["aliq_cofins"])},
        "ipi": {"cst": produto["cst_ipi"], "aliquota": _json_numero(produto["aliq_ipi"])},
        "situacao": produto["situacao"],
        "embasamento_classificacao": produto.get("embasamento_classificacao"),
        "embasamento_tributario": {
            "texto": produto.get("embasamento_legal"),
            "referencia": produto.get("referencia_normativa"),
        },
    }


def _decimal(valor):
    if valor is None or valor == "":
        return None
    return Decimal(str(valor))


def _json_numero(valor):
    if valor is None:
        return None
    numero = Decimal(valor)
    if numero == numero.to_integral():
        return int(numero)
    return float(numero)


def _preenchido(valor) -> bool:
    return valor is not None and str(valor).strip() != ""


def _texto(valor: str) -> str | None:
    texto = (valor or "").strip()
    return texto or None


def _sugestao_publica(linha) -> dict | None:
    if linha is None:
        return None
    return {
        "ncm": linha["ncm_sugerido"],
        "cclass_trib": linha["cclass_sugerido"],
        "analise": linha["analise"],
        "duvida": bool(linha["duvida"]),
    }


def _anotar_descricao_nova(conexao, existente: dict, perfil: dict, arquivo: str) -> None:
    """O EAN publicado passou a vir com outra descrição. O NCM publicado permanece."""
    salvar_sugestao(
        conexao,
        produto_id=existente["id"],
        ean=existente["ean"],
        descricao=perfil["descricao"],
        descricao_norm=perfil["descricao_norm"],
        ncm_atual=existente["ncm"],
        ncm_sugerido=None,
        cclass_sugerido=None,
        analise=None,
        duvida=False,
    )
    _registrar_coleta(conexao, existente["id"], perfil, arquivo)
    conexao.execute(
        "UPDATE produtos SET coletas = coletas + 1, atualizado_em = %s WHERE id = %s",
        (datetime.now(timezone.utc), existente["id"]),
    )


def salvar_sugestao(
    conexao,
    *,
    produto_id,
    ean,
    descricao,
    descricao_norm,
    ncm_atual,
    ncm_sugerido,
    cclass_sugerido,
    analise,
    duvida,
) -> int:
    existente = conexao.execute(
        """
        SELECT id FROM sugestoes
        WHERE situacao = 'pendente'
          AND descricao_norm = %s
          AND produto_id IS NOT DISTINCT FROM %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (descricao_norm, produto_id),
    ).fetchone()
    if existente is not None:
        conexao.execute(
            """
            UPDATE sugestoes
            SET ean = %s, descricao = %s, ncm_atual = %s, ncm_sugerido = %s,
                cclass_sugerido = %s, analise = %s, duvida = %s
            WHERE id = %s
            """,
            (ean, descricao, ncm_atual, ncm_sugerido, cclass_sugerido, analise, duvida, existente["id"]),
        )
        return existente["id"]
    linha = conexao.execute(
        """
        INSERT INTO sugestoes (
            produto_id, ean, descricao, descricao_norm, ncm_atual,
            ncm_sugerido, cclass_sugerido, analise, duvida, situacao
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendente')
        RETURNING id
        """,
        (
            produto_id,
            ean,
            descricao,
            descricao_norm,
            ncm_atual,
            ncm_sugerido,
            cclass_sugerido,
            analise,
            duvida,
        ),
    ).fetchone()
    return linha["id"]


def listar_produtos(busca: str = "", pagina: int = 1, tamanho: int = 40, url: str | None = None) -> dict:
    pagina = max(1, int(pagina or 1))
    tamanho = min(max(int(tamanho or 40), 1), 100)
    termo = normalizar_texto(busca)
    digitos = "".join(caractere for caractere in str(busca or "") if caractere.isdigit())
    filtro = ""
    parametros: list = []
    clausulas = []
    if termo:
        clausulas.append("descricao_norm LIKE %s")
        parametros.append(f"%{termo}%")
    if digitos:
        clausulas.append("COALESCE(ean, '') LIKE %s")
        clausulas.append("COALESCE(ncm, '') LIKE %s")
        clausulas.append("COALESCE(cclass_trib, '') LIKE %s")
        parametros.extend([f"%{digitos}%"] * 3)
    if clausulas:
        filtro = "WHERE " + " OR ".join(clausulas)
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        total = conexao.execute(
            f"SELECT count(*) AS n FROM produtos {filtro}",
            parametros,
        ).fetchone()["n"]
        paginas = max(1, (total + tamanho - 1) // tamanho)
        if pagina > paginas:
            pagina = paginas
        linhas = conexao.execute(
            f"""
            SELECT descricao, ean, ncm, cclass_trib, situacao, cfop_pdv, cfop_nfe
            FROM produtos
            {filtro}
            ORDER BY descricao
            LIMIT %s OFFSET %s
            """,
            [*parametros, tamanho, (pagina - 1) * tamanho],
        ).fetchall()
    return {
        "itens": [dict(linha) for linha in linhas],
        "total": total,
        "pagina": pagina,
        "paginas": paginas,
        "busca": busca or "",
    }


SITUACOES_CADASTRO = ("sanitizado", "conforme", "sem_par", "conflito", "pendente")


def listar_cadastros(situacao: str = "sanitizado", pagina: int = 1, tamanho: int = 40, url: str | None = None) -> dict:
    """Lista os cadastros do último CSV sanitizado."""
    pagina = max(1, int(pagina or 1))
    tamanho = min(max(int(tamanho or 40), 1), 100)
    escolha = situacao if situacao in SITUACOES_CADASTRO or situacao == "todos" else "sanitizado"
    vazio = {
        "itens": [],
        "total": 0,
        "pagina": 1,
        "paginas": 1,
        "situacao": escolha,
        "contagens": {chave: 0 for chave in SITUACOES_CADASTRO},
        "arquivo": "",
        "lidos": 0,
        "quando": "",
    }
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        cabeca = conexao.execute(
            "SELECT * FROM saneamentos ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if cabeca is None:
            return vazio
        contagens = {chave: 0 for chave in SITUACOES_CADASTRO}
        for linha in conexao.execute(
            """
            SELECT situacao, count(*) AS n
            FROM cadastros
            WHERE saneamento_id = %s
            GROUP BY situacao
            """,
            (cabeca["id"],),
        ).fetchall():
            if linha["situacao"] in contagens:
                contagens[linha["situacao"]] = linha["n"]
        filtro = "WHERE saneamento_id = %s"
        parametros: list = [cabeca["id"]]
        if escolha != "todos":
            filtro += " AND situacao = %s"
            parametros.append(escolha)
        total = conexao.execute(
            f"SELECT count(*) AS n FROM cadastros {filtro}",
            parametros,
        ).fetchone()["n"]
        paginas = max(1, (total + tamanho - 1) // tamanho)
        if pagina > paginas:
            pagina = paginas
        linhas = conexao.execute(
            f"""
            SELECT identificador, descricao, ean, ncm_original, ncm, cest_original, cest,
                   cclass_original, cclass, situacao
            FROM cadastros
            {filtro}
            ORDER BY id
            LIMIT %s OFFSET %s
            """,
            [*parametros, tamanho, (pagina - 1) * tamanho],
        ).fetchall()
    quando = cabeca["criado_em"]
    texto_quando = quando.strftime("%d/%m/%Y %H:%M") if hasattr(quando, "strftime") else ""
    return {
        "itens": [dict(linha) for linha in linhas],
        "total": total,
        "pagina": pagina,
        "paginas": paginas,
        "situacao": escolha,
        "contagens": contagens,
        "arquivo": cabeca["arquivo"] or "",
        "lidos": cabeca["lidos"],
        "quando": texto_quando,
    }


def listar_sugestoes_pendentes(url: str | None = None) -> list[dict]:
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        linhas = conexao.execute(
            """
            SELECT id, produto_id, ean, descricao, ncm_atual, ncm_sugerido,
                   cclass_sugerido, analise, duvida
            FROM sugestoes
            WHERE situacao = 'pendente'
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()
    return [dict(linha) for linha in linhas]


def aceitar_sugestao(sugestao_id: int, url: str | None = None) -> bool:
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        sugestao = conexao.execute(
            "SELECT * FROM sugestoes WHERE id = %s AND situacao = 'pendente'",
            (sugestao_id,),
        ).fetchone()
        if sugestao is None or not sugestao["ncm_sugerido"] or not sugestao["cclass_sugerido"]:
            return False
        oficial = conexao.execute(
            "SELECT * FROM cclass_oficial WHERE codigo = %s",
            (sugestao["cclass_sugerido"],),
        ).fetchone()
        vigencia = conexao.execute(
            "SELECT data FROM vigencia_oficial WHERE chave = 'atual'"
        ).fetchone()
        produto = None
        if sugestao["produto_id"] is not None:
            produto = conexao.execute(
                "SELECT * FROM produtos WHERE id = %s",
                (sugestao["produto_id"],),
            ).fetchone()
        if produto is None and sugestao["ean"]:
            produto = conexao.execute(
                "SELECT * FROM produtos WHERE ean = %s",
                (sugestao["ean"],),
            ).fetchone()
        ncm_anterior = None
        situacao = "correto"
        if produto is not None and produto["ncm"] and produto["ncm"] != sugestao["ncm_sugerido"]:
            ncm_anterior = produto["ncm_anterior"] or produto["ncm"]
            situacao = "corrigido"
        elif produto is not None:
            ncm_anterior = produto["ncm_anterior"]
            situacao = produto["situacao"]
        texto = (oficial or {}).get("texto") or (oficial or {}).get("texto_curto")
        referencia = (oficial or {}).get("referencia_normativa")
        descricao_cclass = (oficial or {}).get("descricao")
        tabela_em = vigencia["data"] if vigencia else None
        if produto is None:
            dono = conexao.execute(
                "SELECT id FROM produtos WHERE descricao_norm = %s",
                (sugestao["descricao_norm"],),
            ).fetchone()
            if dono is not None:
                return False
            ean = sugestao["ean"]
            if ean:
                ocupado = conexao.execute(
                    "SELECT id FROM produtos WHERE ean = %s",
                    (ean,),
                ).fetchone()
                if ocupado is not None:
                    ean = None
            conexao.execute(
                """
                INSERT INTO produtos (
                    descricao_norm, descricao, ean, ncm, cclass_trib, descricao_cclass,
                    embasamento_classificacao, embasamento_legal, referencia_normativa,
                    tabela_em, situacao
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'correto')
                """,
                (
                    sugestao["descricao_norm"],
                    sugestao["descricao"],
                    ean,
                    sugestao["ncm_sugerido"],
                    sugestao["cclass_sugerido"],
                    descricao_cclass,
                    sugestao["analise"],
                    texto,
                    referencia,
                    tabela_em,
                ),
            )
        else:
            outra = conexao.execute(
                """
                SELECT id FROM produtos
                WHERE descricao_norm = %s AND id <> %s
                """,
                (sugestao["descricao_norm"], produto["id"]),
            ).fetchone()
            if outra is None and sugestao["descricao_norm"] != produto["descricao_norm"]:
                conexao.execute(
                    """
                    UPDATE produtos
                    SET descricao = %s, descricao_norm = %s, ncm = %s, ncm_anterior = %s,
                        cclass_trib = %s, descricao_cclass = COALESCE(%s, descricao_cclass),
                        embasamento_classificacao = %s, embasamento_legal = %s,
                        referencia_normativa = %s, tabela_em = %s, situacao = %s,
                        atualizado_em = %s
                    WHERE id = %s
                    """,
                    (
                        sugestao["descricao"],
                        sugestao["descricao_norm"],
                        sugestao["ncm_sugerido"],
                        ncm_anterior,
                        sugestao["cclass_sugerido"],
                        descricao_cclass,
                        sugestao["analise"],
                        texto,
                        referencia,
                        tabela_em,
                        situacao,
                        datetime.now(timezone.utc),
                        produto["id"],
                    ),
                )
            else:
                conexao.execute(
                    """
                    UPDATE produtos
                    SET ncm = %s, ncm_anterior = %s, cclass_trib = %s,
                        descricao_cclass = COALESCE(%s, descricao_cclass),
                        embasamento_classificacao = %s, embasamento_legal = %s,
                        referencia_normativa = %s, tabela_em = %s, situacao = %s,
                        atualizado_em = %s
                    WHERE id = %s
                    """,
                    (
                        sugestao["ncm_sugerido"],
                        ncm_anterior,
                        sugestao["cclass_sugerido"],
                        descricao_cclass,
                        sugestao["analise"],
                        texto,
                        referencia,
                        tabela_em,
                        situacao,
                        datetime.now(timezone.utc),
                        produto["id"],
                    ),
                )
        conexao.execute(
            "UPDATE sugestoes SET situacao = 'aceita' WHERE id = %s",
            (sugestao_id,),
        )
        conexao.commit()
    return True


def recusar_sugestao(sugestao_id: int, url: str | None = None) -> bool:
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        alterado = conexao.execute(
            """
            UPDATE sugestoes SET situacao = 'recusada'
            WHERE id = %s AND situacao = 'pendente'
            """,
            (sugestao_id,),
        )
        conexao.commit()
        return alterado.rowcount == 1


def atualizar_texto_legal(conexao, produto: dict, oficial: dict | None, seletivo: dict | None, vigencia) -> None:
    texto = None
    referencia = None
    descricao_cclass = None
    reducoes = {}
    if oficial is not None:
        texto = oficial.get("texto") or oficial.get("texto_curto")
        referencia = oficial.get("referencia_normativa")
        descricao_cclass = oficial.get("descricao") or None
        for campo in ("reducao_cbs", "reducao_ibs_uf", "reducao_ibs_mun"):
            if oficial.get(campo) is not None:
                reducoes[campo] = oficial[campo]
    imposto = None
    if seletivo is not None:
        imposto = "Sim" if seletivo["tributado"] else "Não"
    atribuicoes = ["tabela_em = %s"]
    parametros = [vigencia]
    if oficial is not None:
        atribuicoes.extend(
            [
                "embasamento_legal = %s",
                "referencia_normativa = %s",
                "descricao_cclass = COALESCE(%s, descricao_cclass)",
            ]
        )
        parametros.extend([texto, referencia, descricao_cclass])
    for campo, valor in reducoes.items():
        atribuicoes.append(f"{campo} = %s")
        parametros.append(valor)
    if imposto is not None:
        atribuicoes.append("imposto_seletivo = %s")
        parametros.append(imposto)
    if not _preenchido(produto.get("cst_ibs_cbs")) and oficial is not None and oficial.get("cst"):
        atribuicoes.append("cst_ibs_cbs = %s")
        parametros.append(oficial["cst"])
    parametros.append(datetime.now(timezone.utc))
    parametros.append(produto["id"])
    conexao.execute(
        f"UPDATE produtos SET {', '.join(atribuicoes)}, atualizado_em = %s WHERE id = %s",
        parametros,
    )
