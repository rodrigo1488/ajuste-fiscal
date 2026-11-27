import csv
import os
import threading
import unittest
import urllib.request
from io import StringIO

from ajuste_fiscal.banco import conectar, limpar, listar_cadastros
from ajuste_fiscal.sanitizar import sanitizar_csv
from app import renderizar, criar_servidor

URL_TESTE = "postgresql://ajuste:ajuste@localhost:5434/ajuste_fiscal_teste"
EAN = "7891234567895"
EAN_CONFLITO = "7891234567897"
CABECALHO = (
    "Identificador|Descricao|Código de Barras|CFOP PDV|CFOP NFe|NCM|CEST|"
    "Classif. tributária NFe|Classif. tributária Regular|Classif. tributária NFCe"
)


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


def csv_sistema(linhas) -> bytes:
    texto = "\r\n".join([CABECALHO, *linhas]) + "\r\n"
    return texto.encode("latin-1")


def linha(identificador, descricao, ean, cfop, ncm, cest, cclass) -> str:
    return f"{identificador}|{descricao}|{ean}|{cfop}|{cfop}|{ncm}|{cest}|{cclass}|000000|{cclass}"


def campos(conteudo: bytes, descricao: str) -> list[str]:
    texto = conteudo.decode("latin-1")
    for registro in csv.reader(StringIO(texto), delimiter="|"):
        if len(registro) > 1 and registro[1] == descricao:
            return registro
    raise AssertionError(descricao)


class TelaSanitizacaoTeste(unittest.TestCase):
    def test_planilha_nao_entra_no_lugar_do_csv(self):
        with self.assertRaises(ValueError):
            sanitizar_csv(b"PK\x03\x04planilha")

    def test_pagina_mostra_o_csv_o_carregamento_e_os_sanitizados(self):
        pagina = renderizar(
            aba="importar",
            cadastros={
                "itens": [
                    {
                        "identificador": "10",
                        "descricao": "MOSTARDA",
                        "ean": EAN,
                        "ncm_original": "07051900",
                        "ncm": "21033021",
                        "cest_original": "",
                        "cest": "",
                        "cclass_original": "000000",
                        "cclass": "000001",
                        "situacao": "sanitizado",
                    }
                ],
                "total": 1,
                "pagina": 1,
                "paginas": 1,
                "situacao": "sanitizado",
                "contagens": {
                    "sanitizado": 1,
                    "conforme": 0,
                    "sem_par": 0,
                    "conflito": 0,
                    "pendente": 0,
                },
                "arquivo": "Estoque.csv",
                "lidos": 1,
                "quando": "27/11/2026 14:00",
            },
            pacote={"tipo": "sanitizacao", "csv": b"", "charset": "latin-1"},
            token="abc",
        )
        self.assertIn("Sanitizar cadastros", pagina)
        self.assertIn('name="sistema"', pagina)
        self.assertIn('accept=".csv,text/csv"', pagina)
        self.assertIn('id="carregando"', pagina)
        self.assertIn("Lendo o CSV e sanitizando os cadastros", pagina)
        self.assertIn("Gerando o CSV de importação", pagina)
        self.assertIn("MOSTARDA", pagina)
        self.assertIn("07051900", pagina)
        self.assertIn("21033021", pagina)
        self.assertIn("Baixar CSV sanitizado", pagina)
        self.assertIn("Cadastros sanitizados", pagina)


@unittest.skipUnless(banco_disponivel(), "PostgreSQL de teste indisponível")
class SanitizarCsvTeste(unittest.TestCase):
    def setUp(self):
        self.ambiente = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = URL_TESTE
        limpar(URL_TESTE)
        with conectar(URL_TESTE) as conexao:
            conexao.execute(
                """
                INSERT INTO produtos (descricao_norm, descricao, ean, ncm, cclass_trib, situacao)
                VALUES
                    ('MOSTARDA', 'MOSTARDA', NULL, '21033021', '000001', 'correto'),
                    ('ACUCAR', 'ACUCAR', NULL, '17019900', '200003', 'correto'),
                    ('VINAGRE', 'VINAGRE', NULL, '22090000', '000001', 'correto')
                """
            )
            produto = conexao.execute(
                "SELECT id FROM produtos WHERE descricao_norm = 'VINAGRE'"
            ).fetchone()
            conexao.execute(
                """
                INSERT INTO conflitos (produto_id, descricao_norm, campo, valor_publicado, valor_recebido)
                VALUES (%s, 'VINAGRE', 'ncm', '22090000', '21039021')
                """,
                (produto["id"],),
            )
            conexao.commit()

    def tearDown(self):
        if self.ambiente is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.ambiente

    def test_sanitiza_o_csv_e_sobe_o_que_a_base_nao_tinha(self):
        cru_vinagre = linha("30", "VINAGRE", EAN_CONFLITO, "5102", "21039021", "", "000000")
        arquivo = csv_sistema(
            [
                linha("10", "MOSTARDA", EAN, "5102", "07051900", "", "000000"),
                linha("20", "ACUCAR", "", "5102", "17019900", "", "200003"),
                cru_vinagre,
                linha("40", "SABONETE", "", "5102", "34011190", "", "000000"),
            ]
        )
        resultado = sanitizar_csv(arquivo, "Estoque.csv", URL_TESTE)
        self.assertEqual(resultado.lidos, 4)
        self.assertEqual(resultado.sanitizados, 1)
        self.assertEqual(resultado.conformes, 1)
        self.assertEqual(resultado.conflitos, 1)
        self.assertEqual(resultado.sem_par, 1)
        mostarda = campos(resultado.csv_bytes, "MOSTARDA")
        self.assertEqual(mostarda[5], "21033021")
        self.assertEqual(mostarda[7], "000001")
        self.assertEqual(mostarda[8], "000000")
        self.assertEqual(mostarda[9], "000001")
        self.assertEqual(mostarda[3], "5102")
        self.assertIn(cru_vinagre, resultado.csv_bytes.decode("latin-1"))
        with conectar(URL_TESTE) as conexao:
            publicada = conexao.execute(
                "SELECT ncm, ean, cfop_pdv FROM produtos WHERE descricao_norm = 'MOSTARDA'"
            ).fetchone()
            vinagre = conexao.execute(
                "SELECT ncm, ean FROM produtos WHERE descricao_norm = 'VINAGRE'"
            ).fetchone()
            ausente = conexao.execute(
                "SELECT id FROM produtos WHERE descricao_norm = 'SABONETE'"
            ).fetchone()
            sugestao = conexao.execute(
                "SELECT ncm_atual, ncm_sugerido FROM sugestoes WHERE descricao_norm = 'SABONETE'"
            ).fetchone()
        self.assertEqual(publicada["ncm"], "21033021")
        self.assertEqual(publicada["ean"], EAN)
        self.assertEqual(publicada["cfop_pdv"], "5102")
        self.assertEqual(vinagre["ncm"], "22090000")
        self.assertIsNone(vinagre["ean"])
        self.assertIsNone(ausente)
        self.assertEqual(sugestao["ncm_atual"], "34011190")
        self.assertIsNone(sugestao["ncm_sugerido"])
        lista = listar_cadastros("sanitizado", url=URL_TESTE)
        self.assertEqual([item["descricao"] for item in lista["itens"]], ["MOSTARDA"])
        self.assertEqual(lista["itens"][0]["ncm_original"], "07051900")
        self.assertEqual(lista["itens"][0]["ncm"], "21033021")
        self.assertEqual(lista["contagens"]["sem_par"], 1)
        self.assertEqual(listar_cadastros("conforme", url=URL_TESTE)["itens"][0]["descricao"], "ACUCAR")

        repetido = sanitizar_csv(
            csv_sistema([linha("40", "SABONETE", "", "5102", "34011190", "", "000000")]),
            "outro.csv",
            URL_TESTE,
        )
        self.assertEqual(repetido.sem_par, 1)
        self.assertEqual(listar_cadastros("todos", url=URL_TESTE)["total"], 1)
        self.assertEqual(listar_cadastros("todos", url=URL_TESTE)["arquivo"], "outro.csv")

    def test_endpoint_sanitiza_e_mostra_a_lista(self):
        arquivo = csv_sistema([linha("10", "MOSTARDA", EAN, "5102", "07051900", "", "000000")])
        limite = "----SanitizarTeste"
        corpo = (
            f"--{limite}\r\n"
            'Content-Disposition: form-data; name="sistema"; filename="Estoque.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("latin-1") + arquivo + f"\r\n--{limite}--\r\n".encode("latin-1")
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            pedido = urllib.request.Request(
                f"http://127.0.0.1:{porta}/importar",
                data=corpo,
                method="POST",
            )
            pedido.add_header("Content-Type", f"multipart/form-data; boundary={limite}")
            with urllib.request.urlopen(pedido) as resposta:
                pagina = resposta.read().decode("utf-8")
                endereco = resposta.geturl()
            self.assertIn("aba=importar", endereco)
            self.assertIn("MOSTARDA", pagina)
            self.assertIn("Sanitizado", pagina)
            self.assertIn("Baixar CSV sanitizado", pagina)
            self.assertIn('id="carregando"', pagina)
            with urllib.request.urlopen(endereco.replace("/?", "/baixar/csv?").split("&aba=")[0]) as baixado:
                conteudo = baixado.read()
                disposicao = baixado.headers.get("Content-Disposition", "")
            self.assertEqual(campos(conteudo, "MOSTARDA")[5], "21033021")
            self.assertIn("estoque-sanitizado.csv", disposicao)
        finally:
            servidor.shutdown()
            servidor.server_close()


if __name__ == "__main__":
    unittest.main()
