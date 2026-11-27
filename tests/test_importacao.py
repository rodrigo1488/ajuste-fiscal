import json
import os
import threading
import unittest
import urllib.request
from io import BytesIO
from pathlib import Path

import openpyxl

from ajuste_fiscal.banco import conectar, limpar, listar_produtos
from ajuste_fiscal.importacao import importar_para_base, ler_importacao
from ajuste_fiscal.normalizar import normalizar_texto
from app import criar_servidor

URL_TESTE = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal_teste"


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


def aba_importacao(linhas) -> bytes:
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Importacao"
    aba.append(
        [
            "codigo_produto",
            "descricao",
            "ncm",
            "cst_ibs_cbs",
            "cclasstrib",
            "p_red_cbs",
            "p_red_ibs_uf",
            "p_red_ibs_mun",
            "imposto_seletivo",
            "status_auditoria",
            "ncm_antigo",
            "ncm_sugerido",
        ]
    )
    for linha in linhas:
        aba.append(linha)
    buffer = BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def produto(descricao):
    with conectar(URL_TESTE) as conexao:
        return conexao.execute(
            "SELECT * FROM produtos WHERE descricao_norm = %s",
            (normalizar_texto(descricao),),
        ).fetchone()


@unittest.skipUnless(banco_disponivel(), "PostgreSQL de teste indisponível")
class ImportacaoTeste(unittest.TestCase):
    def setUp(self):
        self.ambiente = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = URL_TESTE
        limpar(URL_TESTE)

    def tearDown(self):
        if self.ambiente is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.ambiente

    def test_grava_definitivos_e_deixa_revisar_de_fora(self):
        arquivo = aba_importacao(
            [
                ["1120", "ZAP COLA", "22021000", "000", "000001", 0, 0, 0, "S", "CORRETO", "22021000", None],
                ["1301", "VEJA", "34029090", "000", "000001", 0, 0, 0, "N", "INCORRETO", "34022000", "34029090"],
                ["264", "TORRESMO", "02091011", "200", "200003", 100, 100, 100, "N", "REVISAR", "02031100", "02091011"],
                ["1448", "ZZ TRIPAS", None, None, None, None, None, None, "N", "NAO_MERCADORIA", "05040013", None],
            ]
        )
        resumo = importar_para_base(arquivo, "importacao.xlsx", URL_TESTE)
        self.assertEqual(resumo.inseridos, 2)
        self.assertEqual(resumo.ignorados, 2)
        self.assertEqual(resumo.conflitos, 0)
        cola = produto("ZAP COLA")
        self.assertEqual(cola["ncm"], "22021000")
        self.assertEqual(cola["cclass_trib"], "000001")
        self.assertEqual(cola["imposto_seletivo"], "Sim")
        self.assertEqual(cola["situacao"], "correto")
        self.assertIsNone(cola["ncm_anterior"])
        veja = produto("VEJA")
        self.assertEqual(veja["ncm"], "34029090")
        self.assertEqual(veja["ncm_anterior"], "34022000")
        self.assertEqual(veja["situacao"], "corrigido")
        self.assertEqual(veja["imposto_seletivo"], "Não")
        self.assertIsNone(produto("TORRESMO"))
        self.assertIsNone(produto("ZZ TRIPAS"))

        repetida = importar_para_base(arquivo, "importacao.xlsx", URL_TESTE)
        self.assertEqual(repetida.inseridos, 0)
        self.assertEqual(repetida.atualizados, 2)
        self.assertEqual(repetida.conflitos, 0)

        divergente = aba_importacao(
            [["1301", "VEJA", "19059090", "000", "000001", 0, 0, 0, "N", "INCORRETO", "34022000", "19059090"]]
        )
        conflito = importar_para_base(divergente, "outra.xlsx", URL_TESTE)
        self.assertEqual(conflito.conflitos, 1)
        self.assertEqual(produto("VEJA")["ncm"], "34029090")

    def test_planilha_auditada_tambem_entra_direto(self):
        livro = openpyxl.Workbook()
        aba = livro.active
        aba.title = "Produtos auditados"
        aba.append(
            ["Código", "Descrição", "ORCCA | Status NCM", "NCM para cadastro", "cClassTrib", "Análise ORCCA"]
        )
        aba.append([1, "MOSTARDA", "Correto", "21033021", "000001", "Mostarda preparada."])
        buffer = BytesIO()
        livro.save(buffer)
        resumo = importar_para_base(buffer.getvalue(), "auditoria.xlsx", URL_TESTE)
        self.assertEqual(resumo.inseridos, 1)
        self.assertEqual(produto("MOSTARDA")["embasamento_classificacao"], "Mostarda preparada.")

    def test_aba_real_separa_definitivos(self):
        caminho = Path(r"C:\Users\Gomes\Downloads\Planilha_SaoCarlos.xlsx")
        if not caminho.exists():
            self.skipTest("planilha de exemplo ausente")
        from ajuste_fiscal.importacao import _ler_aba

        itens = _ler_aba(caminho)
        definitivos = [item for item in itens if item.definitivo]
        self.assertEqual(len(itens), 886)
        self.assertEqual(len(definitivos), 723)
        veja = next(item for item in itens if item.codigo == "1301")
        self.assertEqual(veja.ncm, "34029090")
        self.assertEqual(veja.ncm_anterior, "34022000")
        self.assertEqual(veja.imposto_seletivo, "Não")
        self.assertFalse(next(item for item in itens if item.codigo == "264").definitivo)

    def test_endpoint_importa_e_mostra_o_resumo(self):
        arquivo = aba_importacao(
            [["10", "MOSTARDA", "21033021", "000", "000001", 0, 0, 0, "N", "CORRETO", None, None]]
        )
        limite = "----ImportacaoTeste"
        corpo = (
            f"--{limite}\r\n"
            'Content-Disposition: form-data; name="produtos"; filename="importacao.xlsx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode("utf-8") + arquivo + f"\r\n--{limite}--\r\n".encode("utf-8")
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/") as resposta:
                pagina = resposta.read().decode("utf-8")
            self.assertIn("Importar produtos", pagina)
            pedido = urllib.request.Request(
                f"http://127.0.0.1:{porta}/importar",
                data=corpo,
                method="POST",
            )
            pedido.add_header("Content-Type", f"multipart/form-data; boundary={limite}")
            with urllib.request.urlopen(pedido) as resposta:
                pagina = resposta.read().decode("utf-8")
            self.assertIn("Novos na base", pagina)
            self.assertEqual(produto("MOSTARDA")["ncm"], "21033021")
        finally:
            servidor.shutdown()
            servidor.server_close()

    def test_lista_filtra_e_pagina(self):
        importar_para_base(
            aba_importacao(
                [
                    ["1", "ZAP COLA", "22021000", "000", "000001", 0, 0, 0, "S", "CORRETO", None, None],
                    ["2", "MOSTARDA", "21033021", "000", "000001", 0, 0, 0, "N", "CORRETO", None, None],
                    ["3", "ACUCAR", "17019900", "200", "200003", 0, 0, 0, "N", "CORRETO", None, None],
                ]
            ),
            "lista.xlsx",
            URL_TESTE,
        )
        tudo = listar_produtos("", 1, 2, URL_TESTE)
        self.assertEqual(tudo["total"], 3)
        self.assertEqual(tudo["paginas"], 2)
        self.assertEqual(len(tudo["itens"]), 2)
        segunda = listar_produtos("", 2, 2, URL_TESTE)
        self.assertEqual(len(segunda["itens"]), 1)
        achados = listar_produtos("mostarda", url=URL_TESTE)
        self.assertEqual([item["descricao"] for item in achados["itens"]], ["MOSTARDA"])
        por_ncm = listar_produtos("22021000", url=URL_TESTE)
        self.assertEqual(por_ncm["itens"][0]["ncm"], "22021000")

    def test_pagina_lista_os_produtos_na_aba(self):
        importar_para_base(
            aba_importacao(
                [["10", "MOSTARDA", "21033021", "000", "000001", 0, 0, 0, "N", "CORRETO", None, None]]
            ),
            "lista.xlsx",
            URL_TESTE,
        )
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/?aba=produtos&q=mostarda") as resposta:
                pagina = resposta.read().decode("utf-8")
            self.assertIn(">Produtos</a>", pagina)
            self.assertIn("MOSTARDA", pagina)
            self.assertIn("21033021", pagina)
            self.assertIn('class="aba ativa"', pagina)
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/v1/produtos?q=mostarda") as resposta:
                corpo = json.loads(resposta.read().decode("utf-8"))
            self.assertEqual(corpo["total"], 1)
            self.assertEqual(corpo["itens"][0]["ncm"], "21033021")
        finally:
            servidor.shutdown()
            servidor.server_close()

    def test_arquivo_sem_aba_conhecida_falha(self):
        livro = openpyxl.Workbook()
        livro.active.title = "Outra"
        buffer = BytesIO()
        livro.save(buffer)
        with self.assertRaises(ValueError):
            ler_importacao(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
