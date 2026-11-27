import io
import json
import os
import threading
import unittest
import urllib.request
from decimal import Decimal
from io import BytesIO

import openpyxl

from ajuste_fiscal import processar
from ajuste_fiscal.banco import (
    aceitar_sugestao,
    conectar,
    consultar_ean,
    garantir_schema,
    limpar,
    listar_sugestoes_pendentes,
    recusar_sugestao,
)
from ajuste_fiscal.classificar import conferir, sugerir_lote, sugerir_publico
from ajuste_fiscal.normalizar import normalizar_texto
from ajuste_fiscal.tabelas import sincronizar
from app import criar_servidor

URL_TESTE = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal_teste"
EAN = "7891234567895"


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


def baixar_falso(url: str, classes=None):
    if "nomenclatura" in url:
        return {
            "Nomenclaturas": [
                {"Codigo": "22021000", "Descricao": "Aguas, incluindo aguas minerais e aguas gaseificadas"},
                {"Codigo": "02012090", "Descricao": "Carnes de bovino frescas, costelas"},
            ]
        }
    if "classificacoes-tributarias" in url:
        return classes or [
            {
                "codigo": "000001",
                "descricao": "Situações tributadas integralmente pelo IBS e CBS.",
                "codigoSituacaoTributaria": "000",
                "possuiPercentualReducao": False,
            }
        ]
    if "fundamentacoes" in url:
        return [
            {
                "codigoClassificacaoTributaria": "000001",
                "texto": "Regime geral do IBS e da CBS.",
                "textoCurto": "Regime Geral",
                "referenciaNormativa": "LC 214/2025, Livro I",
                "codigoSituacaoTributaria": "000",
            }
        ]
    if "aliquota" in url:
        return {"aliquotaReferencia": "0.9"}
    if "dados-abertos/ncm" in url:
        return {"tributadoPeloImpostoSeletivo": False}
    raise AssertionError(url)


def resposta_modelo(ncm="22021000", cclass="000001", analise="Bebida pronta.", duvida=False):
    def gerar(_pedido):
        return json.dumps(
            {"ncm": ncm, "cclass_trib": cclass, "analise": analise, "duvida": duvida}
        )

    return gerar


def inserir(descricao, ncm, cclass="000001", ean=EAN, reducao=None, cst="000"):
    with conectar(URL_TESTE) as conexao:
        garantir_schema(conexao, URL_TESTE)
        conexao.execute(
            """
            INSERT INTO produtos (
                descricao_norm, descricao, ean, ncm, cclass_trib, cst_ibs_cbs,
                situacao, reducao_cbs
            ) VALUES (%s, %s, %s, %s, %s, %s, 'correto', %s)
            """,
            (normalizar_texto(descricao), descricao, ean, ncm, cclass, cst, reducao),
        )
        conexao.commit()


def produto(descricao):
    with conectar(URL_TESTE) as conexao:
        return conexao.execute(
            "SELECT * FROM produtos WHERE descricao_norm = %s",
            (normalizar_texto(descricao),),
        ).fetchone()


@unittest.skipUnless(banco_disponivel(), "PostgreSQL de teste indisponível")
class ClassificacaoTeste(unittest.TestCase):
    def setUp(self):
        self.ambiente = {
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "AJUSTE_GRAVAR_BASE": os.environ.get("AJUSTE_GRAVAR_BASE"),
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
        }
        os.environ["DATABASE_URL"] = URL_TESTE
        os.environ["AJUSTE_GRAVAR_BASE"] = "0"
        os.environ.pop("GEMINI_API_KEY", None)
        limpar(URL_TESTE)

    def tearDown(self):
        for chave, valor in self.ambiente.items():
            if valor is None:
                os.environ.pop(chave, None)
            else:
                os.environ[chave] = valor

    def test_conferir_sem_tabela_oficial_nao_altera_os_produtos(self):
        inserir("AGUA TONICA", "22021000")
        with conectar(URL_TESTE) as conexao:
            conexao.execute("DELETE FROM ncm_oficial")
            conexao.commit()
        resumo = conferir(URL_TESTE, gerar=resposta_modelo())
        self.assertIn("tabelas oficiais", resumo.erro or "")
        self.assertEqual(resumo.sugestoes, 0)
        self.assertEqual(produto("AGUA TONICA")["ncm"], "22021000")

    def test_ncm_vigente_atualiza_o_texto_legal_sem_chamar_o_gemini(self):
        inserir("AGUA TONICA", "22021000", reducao=Decimal("40"))
        self._sincronizar()

        def proibido(_pedido):
            raise AssertionError("o Gemini não deveria classificar um NCM vigente")

        resumo = conferir(URL_TESTE, gerar=proibido)
        self.assertEqual(resumo.atualizados, 1)
        self.assertEqual(resumo.sugestoes, 0)
        gravado = produto("AGUA TONICA")
        self.assertEqual(gravado["ncm"], "22021000")
        self.assertEqual(gravado["reducao_cbs"], Decimal("40"))
        self.assertEqual(gravado["embasamento_legal"], "Regime geral do IBS e da CBS.")
        self.assertEqual(gravado["referencia_normativa"], "LC 214/2025, Livro I")
        self.assertEqual(gravado["tabela_em"].isoformat(), "2026-11-20")

    def test_reducao_oficial_entra_quando_a_calculadora_informa_o_numero(self):
        inserir("AGUA TONICA", "22021000", reducao=Decimal("40"))
        classes = [
            {
                "codigo": "000001",
                "descricao": "Situações tributadas integralmente pelo IBS e CBS.",
                "codigoSituacaoTributaria": "000",
                "percentualReducaoCbs": 100,
            }
        ]
        self._sincronizar(classes)
        conferir(URL_TESTE, gerar=resposta_modelo())
        self.assertEqual(produto("AGUA TONICA")["reducao_cbs"], Decimal("100"))

    def test_ncm_extinto_gera_sugestao_e_preserva_o_publicado(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        resumo = conferir(URL_TESTE, gerar=resposta_modelo(analise="Passa a água aromatizada."))
        self.assertEqual(resumo.sugestoes, 1)
        self.assertEqual(produto("REFRIGERANTE")["ncm"], "99999999")
        pendente = listar_sugestoes_pendentes(URL_TESTE)[0]
        self.assertEqual(pendente["ncm_sugerido"], "22021000")
        self.assertEqual(pendente["analise"], "Passa a água aromatizada.")

    def test_codigo_inventado_e_rejeitado(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        resumo = conferir(URL_TESTE, gerar=resposta_modelo(ncm="12345678"))
        self.assertEqual(resumo.rejeitadas, 1)
        self.assertEqual(resumo.sugestoes, 0)
        self.assertEqual(produto("REFRIGERANTE")["ncm"], "99999999")
        self.assertEqual(listar_sugestoes_pendentes(URL_TESTE), [])

    def test_duvida_nao_substitui_o_cadastro_publicado(self):
        inserir("PREPARADO DE CARNE", "99999999")
        self._sincronizar()
        conferir(
            URL_TESTE,
            gerar=resposta_modelo(ncm="02012090", analise="Pode ser costela ou outro corte.", duvida=True),
        )
        status, corpo = consultar_ean(EAN, URL_TESTE)
        self.assertEqual(status, 200)
        self.assertEqual(corpo["ncm"], "99999999")
        self.assertTrue(corpo["sugestao_pendente"]["duvida"])
        self.assertEqual(corpo["sugestao_pendente"]["ncm"], "02012090")
        self.assertNotEqual(corpo["ncm"], corpo["sugestao_pendente"]["ncm"])

    def test_aceitar_publica_o_ncm_e_o_embasamento(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        conferir(URL_TESTE, gerar=resposta_modelo(analise="Bebida pronta."))
        sugestao = listar_sugestoes_pendentes(URL_TESTE)[0]
        self.assertTrue(aceitar_sugestao(sugestao["id"], URL_TESTE))
        status, corpo = consultar_ean(EAN, URL_TESTE)
        self.assertEqual(status, 200)
        self.assertEqual(corpo["ncm"], "22021000")
        self.assertEqual(corpo["embasamento_classificacao"], "Bebida pronta.")
        self.assertEqual(corpo["embasamento_tributario"]["referencia"], "LC 214/2025, Livro I")
        self.assertEqual(corpo["embasamento_tributario"]["texto"], "Regime geral do IBS e da CBS.")
        self.assertIsNone(corpo["sugestao_pendente"])

    def test_recusar_mantem_o_ncm_publicado(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        conferir(URL_TESTE, gerar=resposta_modelo())
        sugestao = listar_sugestoes_pendentes(URL_TESTE)[0]
        self.assertTrue(recusar_sugestao(sugestao["id"], URL_TESTE))
        self.assertEqual(produto("REFRIGERANTE")["ncm"], "99999999")
        self.assertEqual(listar_sugestoes_pendentes(URL_TESTE), [])

    def test_sem_chave_nao_chama_modelo_e_nao_troca_o_ncm(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        resumo = conferir(URL_TESTE)
        self.assertEqual(resumo.sem_chave, 1)
        self.assertEqual(produto("REFRIGERANTE")["ncm"], "99999999")

    def test_descricao_nova_no_mesmo_ean_nao_troca_o_ncm(self):
        inserir("MOSTARDA", "22021000")
        self._sincronizar()
        os.environ["AJUSTE_GRAVAR_BASE"] = "1"
        processar(io.BytesIO(planilha("MOSTARDA ESCURA", "21033021", "000001", "Outro produto.")), csv_sistema())
        gravado = produto("MOSTARDA")
        self.assertEqual(gravado["ncm"], "22021000")
        self.assertEqual(gravado["descricao"], "MOSTARDA")
        pendente = listar_sugestoes_pendentes(URL_TESTE)[0]
        self.assertEqual(pendente["descricao"], "MOSTARDA ESCURA")
        self.assertIsNone(pendente["ncm_sugerido"])
        conferir(URL_TESTE, gerar=resposta_modelo(analise="Mostarda preparada."))
        self.assertEqual(produto("MOSTARDA")["ncm"], "22021000")
        self.assertEqual(listar_sugestoes_pendentes(URL_TESTE)[0]["ncm_sugerido"], "22021000")

    def test_analise_da_planilha_entra_e_nao_e_substituida(self):
        os.environ["AJUSTE_GRAVAR_BASE"] = "1"
        processar(io.BytesIO(planilha("MOSTARDA", "21033021", "000001", "Texto A")), csv_sistema("MOSTARDA"))
        self.assertEqual(produto("MOSTARDA")["embasamento_classificacao"], "Texto A")
        processar(io.BytesIO(planilha("MOSTARDA", "21033021", "000001", "Texto B")), csv_sistema("MOSTARDA"))
        self.assertEqual(produto("MOSTARDA")["embasamento_classificacao"], "Texto A")

    def test_sugerir_devolve_o_perfil_quando_confirma_o_publicado(self):
        inserir("AGUA TONICA", "22021000")
        self._sincronizar()
        status, corpo = sugerir_publico(
            "AGUA TONICA",
            "22021000",
            EAN,
            gerar=resposta_modelo(),
            url=URL_TESTE,
        )
        self.assertEqual(status, 200)
        self.assertEqual(corpo["publicado"]["ncm"], "22021000")
        self.assertIsNone(corpo["sugestao"])
        self.assertEqual(listar_sugestoes_pendentes(URL_TESTE), [])

    def test_endpoint_de_sugestao(self):
        inserir("REFRIGERANTE", "99999999")
        self._sincronizar()
        os.environ["GEMINI_API_KEY"] = "teste"
        import ajuste_fiscal.classificar as classificar

        original = classificar.gerar_gemini
        classificar.gerar_gemini = resposta_modelo(analise="Bebida pronta.")
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/") as resposta:
                pagina = resposta.read().decode("utf-8")
            self.assertIn("Conferir cadastros", pagina)
            self.assertIn("Atualizar tabelas oficiais", pagina)
            self.assertIn("POST /v1/consultar", pagina)
            pedido = urllib.request.Request(
                f"http://127.0.0.1:{porta}/v1/sugerir",
                data=json.dumps(
                    {"descricao": "REFRIGERANTE", "ncm_atual": "99999999", "ean": EAN}
                ).encode("utf-8"),
                method="POST",
            )
            pedido.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(pedido) as resposta:
                corpo = json.loads(resposta.read().decode("utf-8"))
            self.assertEqual(corpo["publicado"]["ncm"], "99999999")
            self.assertEqual(corpo["sugestao"]["ncm"], "22021000")
            self.assertEqual(corpo["sugestao"]["duvida"], False)
        finally:
            classificar.gerar_gemini = original
            servidor.shutdown()
            servidor.server_close()

    def test_conferencia_em_lote_envia_a_tabela_uma_vez(self):
        inserir("REFRIGERANTE", "99999999", ean="7891234567895")
        inserir("PICANHA", "88888888", ean="7891234567896")
        self._sincronizar()
        chamadas = []

        def gerar(pedido):
            chamadas.append(pedido)
            return json.dumps(
                [
                    {"indice": 0, "ncm": "22021000", "cclass_trib": "000001", "analise": "Bebida.", "duvida": False},
                    {"indice": 1, "ncm": "02012090", "cclass_trib": "000001", "analise": "Carne.", "duvida": False},
                ]
            )

        resumo = conferir(URL_TESTE, gerar=gerar)
        self.assertEqual(resumo.sugestoes, 2)
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(chamadas[0].count("cClassTrib oficiais:"), 1)
        self.assertIn("REFRIGERANTE", chamadas[0])
        self.assertIn("PICANHA", chamadas[0])
        self.assertEqual(produto("REFRIGERANTE")["ncm"], "99999999")
        self.assertEqual(produto("PICANHA")["ncm"], "88888888")

    def test_sugerir_lote_nao_repete_o_cadastro_igual(self):
        inserir("REFRIGERANTE", "22021000")
        self._sincronizar()

        def gerar(pedido):
            self.assertEqual(pedido.count("cClassTrib oficiais:"), 1)
            self.assertIn("AGUA TONICA", pedido)
            return json.dumps(
                [
                    {"indice": 0, "ncm": "22021000", "cclass_trib": "000001", "analise": "Igual.", "duvida": False},
                    {"indice": 1, "ncm": "22021000", "cclass_trib": "000001", "analise": "Bebida.", "duvida": False},
                ]
            )

        status, corpo = sugerir_lote(
            [
                {"descricao": "REFRIGERANTE", "ean": EAN, "ncm_atual": "22021000"},
                {"descricao": "AGUA TONICA", "ncm_atual": "99999999"},
            ],
            gerar=gerar,
            url=URL_TESTE,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(corpo["itens"][0]["sugestao"])
        self.assertEqual(corpo["itens"][0]["publicado"]["ncm"], "22021000")
        self.assertNotIn("embasamento_tributario", corpo["itens"][0]["publicado"])
        self.assertEqual(corpo["itens"][1]["sugestao"]["analise"], "Bebida.")
        self.assertEqual(len(listar_sugestoes_pendentes(URL_TESTE)), 1)

    def _sincronizar(self, classes=None):
        resumo = sincronizar(
            "2026-11-20",
            URL_TESTE,
            baixar=lambda url: baixar_falso(url, classes),
        )
        self.assertIsNone(resumo.erro, resumo.erro)
        self.assertGreater(resumo.ncms, 0)


def planilha(descricao, ncm, cclass, analise) -> bytes:
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Produtos auditados"
    aba.append(
        ["Código", "Descrição", "ORCCA | Status NCM", "NCM para cadastro", "cClassTrib", "Análise ORCCA"]
    )
    aba.append([1, descricao, "Correto", ncm, cclass, analise])
    buffer = BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def csv_sistema(descricao="MOSTARDA ESCURA") -> bytes:
    cabecalho = "Identificador|Descricao|Código de Barras|NCM|Classif. tributária NFe|Classif. tributária NFCe"
    linha = f"10|{descricao}|{EAN}|00000000|000000|000000"
    return f"{cabecalho}\r\n{linha}\r\n".encode("latin-1")


if __name__ == "__main__":
    unittest.main()
