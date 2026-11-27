from collections import defaultdict
from dataclasses import dataclass, field

from ajuste_fiscal.contabilidade import ItemContabilidade
from ajuste_fiscal.normalizar import casa_por_embalagem
from ajuste_fiscal.sistema import PlanilhaSistema, gravar_sistema


@dataclass
class Alteracao:
    item: ItemContabilidade
    identificador: str
    descricao_sistema: str
    tipo_casamento: str
    ncm_anterior: str
    ncm_novo: str
    nfe_anterior: str
    nfe_novo: str
    nfce_anterior: str
    nfce_novo: str
    avisos: list[str]
    numero_sistema: int = 0


@dataclass
class Ocorrencia:
    item: ItemContabilidade
    motivo: str
    candidatos: list[str] = field(default_factory=list)


@dataclass
class Resultado:
    itens: list[ItemContabilidade]
    planilha: PlanilhaSistema
    aplicados: list[Alteracao]
    ambiguos: list[Ocorrencia]
    sem_par: list[Ocorrencia]
    excluidos: list[Ocorrencia]
    invalidos: list[Ocorrencia]
    csv_bytes: bytes
    coleta: object | None = None

    @property
    def resumo(self) -> dict[str, int]:
        return {
            "lidos": len(self.itens),
            "definitivos": sum(1 for item in self.itens if item.definitivo),
            "aplicados": len(self.aplicados),
            "ambiguos": len(self.ambiguos),
            "sem_par": len(self.sem_par),
            "excluidos": len(self.excluidos),
            "invalidos": len(self.invalidos),
            "linhas_csv": sum(1 for linha in self.planilha.linhas if linha.campos),
        }


def aplicar(itens: list[ItemContabilidade], planilha: PlanilhaSistema) -> Resultado:
    definitivos = [item for item in itens if item.definitivo]
    excluidos = [
        Ocorrencia(item=item, motivo=_motivo_exclusao(item.status))
        for item in itens
        if not item.definitivo
    ]
    pares, ambiguos, sem_par = _casar(definitivos, planilha)
    alteracoes: dict[int, list[str]] = {}
    aplicados: list[Alteracao] = []
    invalidos: list[Ocorrencia] = []

    for item, linha, tipo in pares:
        avisos = [aviso for aviso in (item.aviso_ncm, item.aviso_cclass) if aviso]
        if item.ncm is None and item.cclass is None:
            invalidos.append(
                Ocorrencia(
                    item=item,
                    motivo="Descrição encontrada no CSV, com NCM e cClassTrib vazios ou inválidos",
                )
            )
            continue
        campos = list(linha.campos)
        while len(campos) <= max(planilha.indice_ncm, planilha.indice_nfe, planilha.indice_nfce):
            campos.append("")
        ncm_anterior = _ler(campos, planilha.indice_ncm)
        nfe_anterior = _ler(campos, planilha.indice_nfe)
        nfce_anterior = _ler(campos, planilha.indice_nfce)
        ncm_novo = ncm_anterior
        nfe_novo = nfe_anterior
        nfce_novo = nfce_anterior
        if item.ncm is not None:
            campos[planilha.indice_ncm] = item.ncm
            ncm_novo = item.ncm
        if item.cclass is not None:
            campos[planilha.indice_nfe] = item.cclass
            campos[planilha.indice_nfce] = item.cclass
            nfe_novo = item.cclass
            nfce_novo = item.cclass
        if campos != list(linha.campos):
            alteracoes[linha.numero] = campos
        aplicados.append(
            Alteracao(
                item=item,
                identificador=linha.identificador,
                descricao_sistema=linha.descricao,
                tipo_casamento=tipo,
                ncm_anterior=ncm_anterior,
                ncm_novo=ncm_novo,
                nfe_anterior=nfe_anterior,
                nfe_novo=nfe_novo,
                nfce_anterior=nfce_anterior,
                nfce_novo=nfce_novo,
                avisos=avisos,
                numero_sistema=linha.numero,
            )
        )

    return Resultado(
        itens=itens,
        planilha=planilha,
        aplicados=aplicados,
        ambiguos=ambiguos,
        sem_par=sem_par,
        excluidos=excluidos,
        invalidos=invalidos,
        csv_bytes=gravar_sistema(planilha, alteracoes),
    )


def _casar(definitivos: list[ItemContabilidade], planilha: PlanilhaSistema):
    por_descricao: dict[str, list[ItemContabilidade]] = defaultdict(list)
    for item in definitivos:
        por_descricao[item.descricao_norm].append(item)

    csv_por_descricao: dict[str, list] = defaultdict(list)
    for linha in planilha.linhas:
        if linha.descricao_norm:
            csv_por_descricao[linha.descricao_norm].append(linha)

    pares = []
    ambiguos: list[Ocorrencia] = []
    sem_par: list[Ocorrencia] = []
    restantes: list[ItemContabilidade] = []
    usados = set()

    for descricao, grupo in por_descricao.items():
        if not descricao:
            sem_par.extend(
                Ocorrencia(item=item, motivo="Descrição vazia na planilha da contabilidade")
                for item in grupo
            )
            continue
        candidatos = csv_por_descricao.get(descricao, [])
        if len(grupo) == 1 and len(candidatos) == 1:
            pares.append((grupo[0], candidatos[0], "exata"))
            usados.add(candidatos[0].numero)
        elif len(grupo) > 1 and candidatos:
            ambiguos.extend(
                Ocorrencia(
                    item=item,
                    motivo="Descrição repetida na contabilidade e presente no CSV",
                    candidatos=_rotulos(candidatos),
                )
                for item in grupo
            )
        elif len(candidatos) > 1:
            ambiguos.append(
                Ocorrencia(
                    item=grupo[0],
                    motivo="Mais de um produto do sistema com a mesma descrição",
                    candidatos=_rotulos(candidatos),
                )
            )
        else:
            restantes.extend(grupo)

    desejos: dict[int, list[tuple[ItemContabilidade, object]]] = defaultdict(list)
    for item in restantes:
        candidatos = []
        for linha in planilha.linhas:
            if linha.numero in usados or not linha.descricao_norm:
                continue
            if casa_por_embalagem(item.descricao_norm, linha.descricao_norm):
                candidatos.append(linha)
        if len(candidatos) == 1:
            desejos[candidatos[0].numero].append((item, candidatos[0]))
        elif len(candidatos) == 0:
            sem_par.append(Ocorrencia(item=item, motivo="Nenhuma descrição correspondente no CSV"))
        else:
            ambiguos.append(
                Ocorrencia(
                    item=item,
                    motivo="Mais de um produto do sistema difere só pela embalagem",
                    candidatos=_rotulos(candidatos),
                )
            )

    for numero, pares_linha in desejos.items():
        if len(pares_linha) == 1:
            item, linha = pares_linha[0]
            pares.append((item, linha, "embalagem"))
        else:
            ambiguos.extend(
                Ocorrencia(
                    item=item,
                    motivo="Vários produtos da contabilidade caem no mesmo item do sistema",
                    candidatos=_rotulos([linha]),
                )
                for item, linha in pares_linha
            )

    return pares, ambiguos, sem_par


def _motivo_exclusao(status: str) -> str:
    if not status:
        return "Sem status de auditoria"
    return f"Fora do cadastro definitivo ({status})"


def _ler(campos: list[str], indice: int) -> str:
    if indice < 0 or indice >= len(campos):
        return ""
    return campos[indice]


def _rotulos(linhas) -> list[str]:
    rotulos = []
    for linha in linhas:
        ident = linha.identificador or "sem identificador"
        rotulos.append(f"{ident} — {linha.descricao}")
    return rotulos
