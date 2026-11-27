"""Cópia local das tabelas oficiais de NCM, cClassTrib e fundamentação."""

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.request import Request, urlopen

from ajuste_fiscal.banco import conectar, garantir_schema

URL_SISCOMEX = "https://portalunico.siscomex.gov.br/classif/api/publico/nomenclatura/download/json"
URL_CALCULADORA = os.environ.get(
    "CALCULADORA_URL",
    "https://piloto-cbs.tributos.gov.br/servico/calculadora-consumo/api/calculadora",
)


@dataclass
class ResumoTabelas:
    ncms: int = 0
    cclasses: int = 0
    aliquotas: int = 0
    seletivos: int = 0
    vigencia: str = ""
    erro: str | None = None

    def como_dict(self) -> dict:
        return {
            "ncms": self.ncms,
            "cclasses": self.cclasses,
            "aliquotas": self.aliquotas,
            "seletivos": self.seletivos,
            "vigencia": self.vigencia,
            "erro": self.erro,
        }


def sincronizar(data: str | None = None, url: str | None = None, baixar=None) -> ResumoTabelas:
    vigencia = _data(data)
    obter = baixar or baixar_json
    base = URL_CALCULADORA.rstrip("/")
    consulta = f"data={vigencia.isoformat()}"
    resumo = ResumoTabelas(vigencia=vigencia.isoformat())
    try:
        ncms = list(_ncms(obter(URL_SISCOMEX)))
        if not ncms:
            raise ValueError("A tabela de NCM do Siscomex veio vazia.")
        classes = _lista(obter(f"{base}/dados-abertos/classificacoes-tributarias/cbs-ibs?{consulta}"))
        if not classes:
            raise ValueError("A tabela de cClassTrib da Calculadora veio vazia.")
        fundamentos = _lista(obter(f"{base}/dados-abertos/fundamentacoes-legais?{consulta}"))
        aliquotas = _aliquotas(obter, base, consulta)
        with conectar(url) as conexao:
            garantir_schema(conexao, url)
            codigos = [
                linha["ncm"]
                for linha in conexao.execute(
                    "SELECT DISTINCT ncm FROM produtos WHERE ncm IS NOT NULL"
                ).fetchall()
            ]
        seletivos = _seletivos(obter, base, consulta, codigos)
        _gravar(url, vigencia, ncms, classes, fundamentos, aliquotas, seletivos)
    except Exception as exc:
        resumo.erro = str(exc)
        return resumo
    resumo.ncms = len(ncms)
    resumo.cclasses = len(classes)
    resumo.aliquotas = len(aliquotas)
    resumo.seletivos = len(seletivos)
    return resumo


def baixar_json(url: str):
    pedido = Request(url, headers={"Accept": "application/json", "User-Agent": "ajuste-fiscal"})
    with urlopen(pedido, timeout=120) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


def _data(valor: str | None) -> date:
    if valor:
        return date.fromisoformat(valor)
    ambiente = os.environ.get("TABELA_DATA", "").strip()
    if ambiente:
        return date.fromisoformat(ambiente)
    return date.today()


def _ncms(payload) -> list[tuple[str, str]]:
    lista = payload
    if isinstance(payload, dict):
        lista = (
            payload.get("Nomenclaturas")
            or payload.get("nomenclaturas")
            or payload.get("lista")
            or []
        )
    achados = []
    for item in lista or []:
        if not isinstance(item, dict):
            continue
        codigo = re.sub(r"\D", "", str(item.get("Codigo") or item.get("codigo") or ""))
        descricao = str(item.get("Descricao") or item.get("descricao") or "").strip()
        if len(codigo) == 8 and descricao:
            achados.append((codigo, descricao))
    return achados


def _lista(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for chave in ("dados", "lista", "content", "classificacoes", "items"):
            if isinstance(payload.get(chave), list):
                return [item for item in payload[chave] if isinstance(item, dict)]
        if any(chave in payload for chave in ("codigo", "codigoClassificacaoTributaria", "aliquotaReferencia")):
            return [payload]
    return []


def _aliquotas(obter, base: str, consulta: str) -> dict[str, Decimal]:
    enderecos = {
        "CBS": f"{base}/dados-abertos/aliquota-uniao?{consulta}",
        "IBS_UF": f"{base}/dados-abertos/aliquota-uf?{consulta}",
        "IBS_MUN": f"{base}/dados-abertos/aliquota-municipio?{consulta}",
    }
    achados = {}
    for tributo, endereco in enderecos.items():
        try:
            payload = obter(endereco)
        except Exception:
            continue
        numero = _aliquota(payload)
        if numero is not None:
            achados[tributo] = numero
    return achados


def _aliquota(payload):
    if isinstance(payload, list):
        return _aliquota(payload[0]) if payload else None
    if not isinstance(payload, dict):
        return None
    for chave in ("aliquotaReferencia", "aliquota", "valor"):
        if payload.get(chave) not in (None, ""):
            return Decimal(str(payload[chave]))
    return None


def _seletivos(obter, base: str, consulta: str, ncms: list[str]) -> dict[str, dict]:
    achados = {}
    for ncm in ncms:
        try:
            payload = obter(f"{base}/dados-abertos/ncm?ncm={ncm}&{consulta}")
        except Exception:
            continue
        if not isinstance(payload, dict) or "tributadoPeloImpostoSeletivo" not in payload:
            continue
        achados[ncm] = {
            "tributado": bool(payload.get("tributadoPeloImpostoSeletivo")),
            "ad_valorem": _decimal_opcional(payload.get("aliquotaAdValorem")),
            "ad_rem": _decimal_opcional(payload.get("aliquotaAdRem")),
        }
    return achados


def _gravar(url, vigencia: date, ncms, classes, fundamentos, aliquotas, seletivos) -> None:
    por_codigo = {}
    for item in fundamentos:
        codigo = _codigo_classe(item)
        if codigo:
            por_codigo[codigo] = item
    linhas_classe = []
    for item in classes:
        codigo = _codigo_classe(item)
        if not codigo:
            continue
        fundamento = por_codigo.get(codigo, {})
        reducao_cbs = _reducao(item, "cbs", "Cbs", "CBS")
        reducao_ibs = _reducao(item, "ibs", "Ibs", "IBS")
        linhas_classe.append(
            (
                codigo,
                item.get("descricao") or fundamento.get("descricaoClassificacaoTributaria"),
                _cst(item) or _cst(fundamento),
                fundamento.get("texto") or item.get("texto"),
                fundamento.get("textoCurto") or item.get("textoCurto"),
                fundamento.get("referenciaNormativa") or item.get("referenciaNormativa"),
                bool(item.get("possuiPercentualReducao")),
                reducao_cbs,
                _reducao(item, "ibsUf", "IbsUf", "IBS_UF") or reducao_ibs,
                _reducao(item, "ibsMun", "IbsMun", "IBS_MUN") or reducao_ibs,
                vigencia,
            )
        )
    if not linhas_classe:
        raise ValueError("Nenhum cClassTrib válido na tabela oficial.")
    with conectar(url) as conexao:
        garantir_schema(conexao, url)
        conexao.execute("DELETE FROM ncm_oficial")
        conexao.execute("DELETE FROM cclass_oficial")
        conexao.execute("DELETE FROM aliquota_oficial")
        conexao.execute("DELETE FROM seletivo_oficial")
        _varios(
            conexao,
            "INSERT INTO ncm_oficial (codigo, descricao, vigencia) VALUES (%s, %s, %s)",
            [(codigo, descricao, vigencia) for codigo, descricao in ncms],
        )
        _varios(
            conexao,
            """
            INSERT INTO cclass_oficial (
                codigo, descricao, cst, texto, texto_curto, referencia_normativa,
                possui_reducao, reducao_cbs, reducao_ibs_uf, reducao_ibs_mun, vigencia
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            linhas_classe,
        )
        if aliquotas:
            _varios(
                conexao,
                "INSERT INTO aliquota_oficial (tributo, aliquota, vigencia) VALUES (%s, %s, %s)",
                [(tributo, valor, vigencia) for tributo, valor in aliquotas.items()],
            )
        if seletivos:
            _varios(
                conexao,
                """
                INSERT INTO seletivo_oficial (ncm, tributado, aliquota_ad_valorem, aliquota_ad_rem, vigencia)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (ncm, dados["tributado"], dados["ad_valorem"], dados["ad_rem"], vigencia)
                    for ncm, dados in seletivos.items()
                ],
            )
        conexao.execute(
            """
            INSERT INTO vigencia_oficial (chave, data, atualizado_em)
            VALUES ('atual', %s, now())
            ON CONFLICT (chave) DO UPDATE SET data = EXCLUDED.data, atualizado_em = now()
            """,
            (vigencia,),
        )
        conexao.commit()


def _varios(conexao, comando: str, linhas: list) -> None:
    with conexao.cursor() as cursor:
        cursor.executemany(comando, linhas)


def _codigo_classe(item: dict) -> str | None:
    bruto = item.get("codigo") or item.get("codigoClassificacaoTributaria")
    if bruto is None:
        return None
    codigo = re.sub(r"\D", "", str(bruto))
    if not codigo or len(codigo) > 6:
        return None
    return codigo.zfill(6)


def _cst(item: dict) -> str | None:
    bruto = item.get("codigoSituacaoTributaria") or item.get("cst")
    if bruto is None:
        return None
    codigo = re.sub(r"\D", "", str(bruto))
    if not codigo or len(codigo) > 3:
        return None
    return codigo.zfill(3)


def _reducao(item: dict, *nomes):
    for nome in nomes:
        for chave, valor in item.items():
            if nome.lower() in chave.lower() and "reduc" in chave.lower() and valor not in (None, ""):
                return _decimal_opcional(valor)
    return None


def _decimal_opcional(valor):
    if valor is None or valor == "":
        return None
    return Decimal(str(valor))
