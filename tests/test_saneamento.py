import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date

from ajuste_fiscal.banco import conectar, criar_parceiro, garantir_schema, limpar
from ajuste_fiscal.classificar import conferir
from ajuste_fiscal.saneamento import sanear
from app import criar_servidor

URL_TESTE = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal_teste"
CNPJ = "02627210000130"
EAN = "7891234567895"
EAN_ST = "7891234567896"
EAN_CONFLITO = "7891234567897"


def banco_disponivel() -> bool:
    try:
        import psycopg

        with psycopg.connect("postgresql://ajuste:ajuste@localhost:5434/postgres", autocommit=True) as conexao:
            existe = conexao.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'ajuste_fiscal_teste'"
            ).fetchone()
            if existe is None:
                conexao.execute("CREATE DATABASE ajuste_fiscal_teste")
        return True
    except Exception:
        return False


def pedido(regime="SN", uf="SP", produtos=None, fabrica=False, cfop="5102"):
    return {
        "emit": {"cnpj": CNPJ, "uf": uf, "regime": regime, "crt": 1, "cnae": "4711301"},
        "perfil": {"uf": uf, "cfop": cfop, "finalidade": 0, "fabricacaoPropria": fabrica},
        "produtos": produtos
        if produtos is not None
        else [{"codigo": EAN, "codInterno": "N", "descricao": "MOSTARDA", "ncm": "21033021", "cest": ""}],
    }


@unittest.skipUnless(banco_disponivel(), "PostgreSQL de teste indisponível")
class SaneamentoTeste(unittest.TestCase):
    def setUp(self):
        self.ambiente = {
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
        }
        os.environ["DATABASE_URL"] = URL_TESTE
        os.environ.pop("GEMINI_API_KEY", None)
        limpar(URL_TESTE)
        with conectar(URL_TESTE) as conexao:
            garantir_schema(conexao, URL_TESTE)
            conexao.execute("DELETE FROM parceiros WHERE cnpj = %s", (CNPJ,))
            conexao.execute(
                """
                INSERT INTO cclass_oficial (
                    codigo, descricao, cst, texto, texto_curto, referencia_normativa,
                    possui_reducao, vigencia
                ) VALUES ('000001', 'Integral', '000', %s, 'Regime Geral', 'LC 214/2025', false, %s)
                ON CONFLICT (codigo) DO UPDATE
                SET texto = EXCLUDED.texto, texto_curto = EXCLUDED.texto_curto,
                    cst = EXCLUDED.cst, referencia_normativa = EXCLUDED.referencia_normativa,
                    vigencia = EXCLUDED.vigencia
                """,
                ("TEXTO LONGO DA LEI QUE NAO ENTRA NO SANEAMENTO", date(2026, 11, 20)),
            )
            for tributo, aliquota in (("CBS", "0.9"), ("IBS_UF", "0.1"), ("IBS_MUN", "0")):
                conexao.execute(
                    """
                    INSERT INTO aliquota_oficial (tributo, aliquota, vigencia)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tributo) DO UPDATE SET aliquota = EXCLUDED.aliquota
                    """,
                    (tributo, aliquota, date(2026, 11, 20)),
                )
            conexao.execute(
                """
                INSERT INTO produtos (
                    descricao_norm, descricao, ean, ncm, cest, cclass_trib, cst_ibs_cbs,
                    situacao, embasamento_legal
                ) VALUES
                    ('MOSTARDA', 'MOSTARDA', %s, '21033021', NULL, '000001', '000', 'correto', %s),
                    ('REFRIGERANTE', 'REFRIGERANTE', %s, '22021000', '0301100', '000001', '000', 'correto', NULL),
                    ('VINAGRE', 'VINAGRE', %s, '22090000', NULL, '000001', '000', 'correto', NULL)
                """,
                (EAN, "TEXTO LONGO DA LEI QUE NAO ENTRA NO SANEAMENTO", EAN_ST, EAN_CONFLITO),
            )
            produto = conexao.execute("SELECT id FROM produtos WHERE ean = %s", (EAN_CONFLITO,)).fetchone()
            conexao.execute(
                """
                INSERT INTO conflitos (produto_id, descricao_norm, ean, campo, valor_publicado, valor_recebido)
                VALUES (%s, 'VINAGRE', %s, 'ncm', '22090000', '21039021')
                """,
                (produto["id"], EAN_CONFLITO),
            )
            conexao.commit()
        self.parceiro = criar_parceiro("ERP teste", CNPJ, URL_TESTE)

    def tearDown(self):
        for chave, valor in self.ambiente.items():
            if valor is None:
                os.environ.pop(chave, None)
            else:
                os.environ[chave] = valor

    def test_simples_devolve_csosn_e_omite_o_texto_longo(self):
        corpo = sanear(pedido(), self.parceiro["token"], URL_TESTE)
        self.assertEqual(corpo["cabecalho"]["prodRet"], 1)
        grupo = corpo["grupos"][0]
        self.assertEqual(grupo["ean"], EAN.zfill(14))
        self.assertEqual(grupo["cbs"]["cClassTrib"], "000001")
        self.assertEqual(grupo["regras"][0]["csosn"], "102")
        self.assertEqual(grupo["regras"][0]["cfop"], "5102")
        self.assertFalse(grupo["regras"][0]["st"])
        self.assertEqual(grupo["pisCofins"]["aliqPis"], 0.65)
        self.assertEqual(grupo["pisCofins"]["aliqCofins"], 3)
        self.assertNotIn("TEXTO LONGO DA LEI QUE NAO ENTRA NO SANEAMENTO", json.dumps(corpo))
        self.assertNotIn("cst", grupo["regras"][0])

    def test_cest_usa_a_substituicao(self):
        corpo = sanear(
            pedido(produtos=[{"codigo": EAN_ST, "codInterno": "N", "descricao": "REFRIGERANTE"}]),
            self.parceiro["token"],
            URL_TESTE,
        )
        regra = corpo["grupos"][0]["regras"][0]
        self.assertEqual(regra["cfop"], "5405")
        self.assertEqual(regra["csosn"], "500")
        self.assertTrue(regra["st"])
        self.assertEqual(corpo["grupos"][0]["pisCofins"]["cstSai"], "04")
        self.assertEqual(corpo["grupos"][0]["pisCofins"]["aliqPis"], 0)

    def test_presumido_usa_cst_cumulativo_e_real_usa_nao_cumulativo(self):
        presumido = sanear(pedido(regime="LP"), self.parceiro["token"], URL_TESTE)
        self.assertEqual(presumido["grupos"][0]["regras"][0]["cst"], "00")
        self.assertNotIn("csosn", presumido["grupos"][0]["regras"][0])
        self.assertEqual(presumido["grupos"][0]["pisCofins"]["aliqPis"], 0.65)
        real = sanear(pedido(regime="LR"), self.parceiro["token"], URL_TESTE)
        self.assertEqual(real["grupos"][0]["regras"][0]["cst"], "00")
        self.assertEqual(real["grupos"][0]["pisCofins"]["aliqPis"], 1.65)
        self.assertEqual(real["grupos"][0]["pisCofins"]["aliqCofins"], 7.6)

    def test_conflito_invalido_e_ausente_nao_chamam_o_gemini(self):
        import ajuste_fiscal.classificar as classificar

        def proibido(_pedido):
            raise AssertionError("o saneamento não classifica no pedido")

        original = classificar.gerar_gemini
        classificar.gerar_gemini = proibido
        try:
            corpo = sanear(
                pedido(
                    produtos=[
                        {"codigo": EAN_CONFLITO, "codInterno": "N", "descricao": "VINAGRE"},
                        {"codigo": "abc", "codInterno": "N"},
                        {"codigo": "999", "codInterno": "S", "descricao": "SABONETE NOVO"},
                    ]
                ),
                self.parceiro["token"],
                URL_TESTE,
            )
        finally:
            classificar.gerar_gemini = original
        self.assertEqual(corpo["cabecalho"]["prodEnv"], 3)
        self.assertEqual(corpo["cabecalho"]["prodRet"], 0)
        self.assertEqual(corpo["cabecalho"]["prodNaoRet"], 3)
        motivos = {item["motivo"] for item in corpo["naoRetornados"]}
        self.assertEqual(motivos, {"conflito", "invalido", "ausente"})
        with conectar(URL_TESTE) as conexao:
            gravada = conexao.execute(
                "SELECT ncm_sugerido FROM sugestoes WHERE descricao_norm = 'SABONETE NOVO'"
            ).fetchone()
        self.assertIsNone(gravada["ncm_sugerido"])

    def test_descricao_nova_entra_na_conferencia_sem_publicar(self):
        sanear(
            pedido(produtos=[{"codigo": "1", "codInterno": "S", "descricao": "SABONETE NOVO"}]),
            self.parceiro["token"],
            URL_TESTE,
        )

        def gerar(texto):
            self.assertIn("SABONETE NOVO", texto)
            return json.dumps({"ncm": "22021000", "cclass_trib": "000001", "analise": "Higiene.", "duvida": False})

        with conectar(URL_TESTE) as conexao:
            for codigo, descricao in (
                ("21033021", "Mostarda"),
                ("22021000", "Agua"),
                ("22090000", "Vinagre"),
            ):
                conexao.execute(
                    """
                    INSERT INTO ncm_oficial (codigo, descricao, vigencia)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (codigo) DO NOTHING
                    """,
                    (codigo, descricao, date(2026, 11, 20)),
                )
            conexao.commit()
        resumo = conferir(URL_TESTE, gerar=gerar)
        self.assertGreaterEqual(resumo.sugestoes, 1)
        with conectar(URL_TESTE) as conexao:
            publicada = conexao.execute(
                "SELECT 1 FROM produtos WHERE descricao_norm = 'SABONETE NOVO'"
            ).fetchone()
        self.assertIsNone(publicada)

    def test_endpoint_recusa_token_e_uf_e_preserva_o_ean(self):
        os.environ["DATABASE_URL"] = URL_TESTE
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            sem_token = urllib.request.Request(
                f"http://127.0.0.1:{porta}/v1/saneamento",
                data=json.dumps(pedido()).encode("utf-8"),
                method="POST",
            )
            sem_token.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as erro:
                urllib.request.urlopen(sem_token)
            self.assertEqual(erro.exception.code, 401)
            outro_estado = urllib.request.Request(
                f"http://127.0.0.1:{porta}/v1/saneamento",
                data=json.dumps(pedido(uf="RJ")).encode("utf-8"),
                method="POST",
            )
            outro_estado.add_header("Content-Type", "application/json")
            outro_estado.add_header("Authorization", f"Bearer {self.parceiro['token']}")
            with self.assertRaises(urllib.error.HTTPError) as erro_uf:
                urllib.request.urlopen(outro_estado)
            self.assertEqual(erro_uf.exception.code, 400)
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/v1/ean/{EAN}") as resposta:
                corpo = json.loads(resposta.read().decode("utf-8"))
            self.assertEqual(corpo["ncm"], "21033021")
            self.assertIn("embasamento_tributario", corpo)
            self.assertEqual(resposta.status, 200)
        finally:
            servidor.shutdown()
            servidor.server_close()
