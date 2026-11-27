import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from io import BytesIO

import openpyxl

from ajuste_fiscal import processar
from ajuste_fiscal.banco import conectar, consultar_ean, consultar_lote, limpar, registrar, url_banco
from app import criar_servidor

URL_TESTE = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal_teste"
CABECALHO = (
    "Identificador|Descricao|Código de Barras|CFOP PDV|CFOP NFe|NCM|CEST|"
    "CST NFe|CSOSN NFe|CST COFINS|Aliq. COFINS|CST PIS|Aliq. PIS|CST IPI|Aliq. IPI|"
    "Classif. tributária NFe|Classif. tributária NFCe|Natureza de operação"
)


def planilha(linhas) -> bytes:
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Produtos auditados"
    aba.append(
        ["Código", "Descrição", "NCM", "ORCCA | Status NCM", "NCM para cadastro", "cClassTrib"]
    )
    for linha in linhas:
        aba.append(linha)
    buffer = BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def csv_sistema(linhas) -> bytes:
    texto = "\r\n".join([CABECALHO, *linhas]) + "\r\n"
    return texto.encode("latin-1")


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


@unittest.skipUnless(banco_disponivel(), "PostgreSQL de teste indisponível")
class BancoTeste(unittest.TestCase):
    def setUp(self):
        os.environ["DATABASE_URL"] = URL_TESTE
        limpar(URL_TESTE)

    def tearDown(self):
        os.environ["AJUSTE_GRAVAR_BASE"] = "0"

    def test_grava_repete_e_conflita_sem_revisar(self):
        os.environ["AJUSTE_GRAVAR_BASE"] = "0"
        primeira = self._resultado(
            [
                [1, "MOSTARDA", "07051900", "Incorreto", "21033021", "000001"],
                [2, "AÇÚCAR", "17019900", "Correto", "17019900", "200003"],
                [3, "TORRESMO", "02031100", "Revisar", "02091011", "200003"],
            ],
            ["10|MOSTARDA|7891234567895|5102|5102|07051900|1702200|000|102|01|7.60|01|1.65|99|0|000000|000000|5102 - Venda de mercadoria"],
        )
        resumo = registrar(primeira, "auditoria.xlsx", URL_TESTE)
        self.assertEqual(resumo.inseridos, 2)
        self.assertEqual(resumo.conflitos, 0)
        self.assertGreaterEqual(resumo.ignorados, 1)

        repetida = registrar(primeira, "auditoria.xlsx", URL_TESTE)
        self.assertEqual(repetida.inseridos, 0)
        self.assertEqual(repetida.atualizados, 2)
        self.assertEqual(repetida.conflitos, 0)

        status, corpo = consultar_ean("7891234567895", URL_TESTE)
        self.assertEqual(status, 200)
        self.assertEqual(corpo["ncm"], "21033021")
        self.assertEqual(corpo["situacao"], "corrigido")
        self.assertEqual(corpo["natureza_operacao"]["codigo"], "5102")
        self.assertEqual(corpo["cfop_pdv"], "5102")
        self.assertEqual(corpo["cest"], "1702200")

        divergente = self._resultado(
            [[1, "MOSTARDA", "07051900", "Incorreto", "19059090", "000001"]],
            ["10|MOSTARDA|7891234567895|5102|5102|07051900||000|102|01|7.60|01|1.65|99|0|000000|000000|"],
        )
        conflito = registrar(divergente, "outra.xlsx", URL_TESTE)
        self.assertEqual(conflito.conflitos, 1)
        status, corpo = consultar_ean("789.1234.5678.95", URL_TESTE)
        self.assertEqual(status, 409)
        self.assertEqual(consultar_ean("123", URL_TESTE)[0], 404)

    def test_consulta_em_lote_omite_o_texto_longo(self):
        os.environ["AJUSTE_GRAVAR_BASE"] = "0"
        os.environ["DATABASE_URL"] = URL_TESTE
        resultado = self._resultado(
            [
                [1, "MOSTARDA", "21033021", "Correto", "21033021", "000001"],
                [2, "AÇÚCAR", "17019900", "Correto", "17019900", "200003"],
            ],
            [
                "10|MOSTARDA|7891234567895|5102|5405|21033021|1702200|000|102|01|7.60|01|1.65||0|000001|000001|5102 - Venda de mercadoria",
                "11|AÇÚCAR|7891234567896|5102|5405|17019900||000|102|01|7.60|01|1.65||0|200003|200003|",
            ],
        )
        registrar(resultado, "ok.xlsx", URL_TESTE)
        with conectar(URL_TESTE) as conexao:
            conexao.execute(
                "UPDATE produtos SET embasamento_legal = %s WHERE ean = %s",
                ("Texto longo da lei complementar que não deve ir no lote curto.", "7891234567895"),
            )
            conexao.commit()
        lote = consultar_lote(
            ["7891234567895", "00000000", "abc", "7891234567895"],
            ["AÇÚCAR", "MOSTARDA"],
            url=URL_TESTE,
        )
        self.assertEqual([item["ncm"] for item in lote["itens"]], ["21033021", "17019900"])
        self.assertNotIn("embasamento_tributario", lote["itens"][0])
        self.assertNotIn("Texto longo da lei", json.dumps(lote, ensure_ascii=False))
        self.assertEqual(lote["itens"][0]["natureza"], "5102")
        self.assertEqual(lote["ausentes"], ["00000000"])
        self.assertEqual(lote["invalidos"], ["abc"])
        divergente = self._resultado(
            [[1, "MOSTARDA", "07051900", "Incorreto", "19059090", "000001"]],
            ["10|MOSTARDA|7891234567895|5102|5102|07051900||000|102|01|7.60|01|1.65|99|0|000000|000000|"],
        )
        registrar(divergente, "outra.xlsx", URL_TESTE)
        bloqueado = consultar_lote(["7891234567895"], url=URL_TESTE)
        self.assertEqual(bloqueado["conflitos"], ["7891234567895"])
        self.assertEqual(bloqueado["itens"], [])

        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            pedido = urllib.request.Request(
                f"http://127.0.0.1:{porta}/v1/consultar",
                data=json.dumps({"eans": ["7891234567896"], "descricoes": ["MOSTARDA"]}).encode("utf-8"),
                method="POST",
            )
            pedido.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(pedido) as resposta:
                corpo = json.loads(resposta.read().decode("utf-8"))
            self.assertEqual(corpo["itens"][0]["ncm"], "17019900")
            self.assertEqual(corpo["conflitos"], ["MOSTARDA"])
        finally:
            servidor.shutdown()
            servidor.server_close()

    def test_endpoint_publico(self):
        os.environ["DATABASE_URL"] = URL_TESTE
        os.environ["AJUSTE_GRAVAR_BASE"] = "0"
        resultado = self._resultado(
            [[1, "MOSTARDA", "21033021", "Correto", "21033021", "000001"]],
            ["10|MOSTARDA|7891234567895|5102|5405|21033021||000|102|01|7.60|01|1.65||0|000001|000001|"],
        )
        registrar(resultado, "ok.xlsx", URL_TESTE)
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/v1/ean/7891234567895") as resposta:
                self.assertEqual(resposta.status, 200)
                corpo = json.loads(resposta.read().decode("utf-8"))
            self.assertEqual(corpo["ncm"], "21033021")
            self.assertEqual(corpo["cfop_nfe"], "5405")
            pedido = urllib.request.Request(f"http://127.0.0.1:{porta}/v1/ean/00000000")
            try:
                urllib.request.urlopen(pedido)
                self.fail("o EAN ausente deveria responder 404")
            except urllib.error.HTTPError as erro:
                self.assertEqual(erro.code, 404)
        finally:
            servidor.shutdown()
            servidor.server_close()

    def _resultado(self, itens, linhas):
        resultado, _relatorio = processar(io.BytesIO(planilha(itens)), csv_sistema(linhas))
        return resultado


if __name__ == "__main__":
    unittest.main()
