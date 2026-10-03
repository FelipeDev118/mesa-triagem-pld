"""Mesa de Triagem PLD - camada de sistema construida sobre a entrega do desafio.

Os modulos de nivel_1/, nivel_2/ e nivel_3/ NAO sao movidos nem renomeados: a
estrutura obrigatoria do enunciado continua valendo como entrega. O que este
pacote faz e dar a eles um store, uma fila e uma trilha de auditoria - o que
falta para um analista usar isto todo dia, em vez de ler CSVs.

Ver .claude/features/ROADMAP.txt para a escada de desenvolvimento.
"""
import sys
from pathlib import Path

# nivel_2/ no sys.path pelo mesmo motivo (e da mesma forma) que nivel_3 faz: os
# modulos da entrega usam imports soltos (`from dados import ...`), porque foram
# escritos para rodar como script de dentro da propria pasta. Reproduzimos isso
# aqui em vez de reescrever a entrega - a logica de negocio nao e duplicada nem
# movida, so alcancada.
_NIVEL_2 = Path(__file__).resolve().parent.parent / "nivel_2"
if str(_NIVEL_2) not in sys.path:
    sys.path.insert(0, str(_NIVEL_2))
