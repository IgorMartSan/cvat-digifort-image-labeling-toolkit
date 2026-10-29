# Coletor de imagens de câmeras

Script único para coletar snapshots HTTP/HTTPS de várias câmeras com intervalos independentes. Também recupera gravações do Digifort. Requer Python 3.12+ e uv.

## Executar

Na pasta `digifort-scripts`, configure o `.env` usando `.env.example` como modelo:

```powershell
uv run python main.py
```

O uv instala as dependências automaticamente. Ctrl+C encerra a coleta. Para incluir câmeras, adicione objetos à lista `CAMERAS` e reinicie.

## Exemplo completo do .env

Recuperar os últimos 30 dias e continuar ao vivo:

```dotenv
CAMERAS='[
  {
    "url": "http://SERVIDOR:8601/Interface/Cameras/GetSnapshot?Camera=CAMERA_01",
    "folder": "camera_01",
    "interval_seconds": 60,
    "history": true,
    "start_date": 30,
    "end_date": null,
    "quality": 100,
    "resize": null,
    "history_search_hours": 6,
    "history_retry_seconds": 30
  }
]'

OUTPUT_DIR=./volume/image
REQUEST_TIMEOUT=10
```

Substitua servidor e nome da câmera. Para autenticação HTTP Basic, pode usar `http://USUARIO:SENHA@SERVIDOR:8601/Interface/Cameras/GetSnapshot?Camera=CAMERA_01`. Codifique caracteres especiais das credenciais, como `@` em `%40` e `&` em `%26`. O `.env` é ignorado pelo Git; URLs e credenciais não aparecem nos logs do coletor.

## Início, fim e exemplos para reutilizar

Os campos abaixo ficam **dentro do objeto de cada câmera**, junto com URL e pasta.

| Objetivo | `start_date` | `end_date` | `history` |
|---|---|---|---|
| Últimos 30 dias e continuar ao vivo | `30` | `null` ou omitido | `true` |
| Últimos 30 dias até agora, depois encerrar | `30` | `0` | `true` |
| Da gravação mais antiga e continuar | `null` ou omitido | `null` ou omitido | `true` |
| Da mais antiga até agora, depois encerrar | `null` ou omitido | `0` | `true` |
| Período fixo | `"2026-10-01T08:00:00"` | `"2026-10-02T18:00:00"` | `true` |
| Apenas ao vivo, sem fim | `null` ou omitido | `null` ou omitido | `false` |
| Ao vivo até um horário futuro | `null` ou omitido | `"2026-10-30T18:00:00"` | `false` |

Exemplo de período fixo com data **e hora**:

```json
{
  "start_date": "2026-10-01T08:30:00",
  "end_date": "2026-10-02T18:45:00",
  "history": true
}
```

O fragmento mostra somente o período; mantenha os campos obrigatórios da câmera. Datas usam ISO 8601. Sem fuso explícito, seguem o horário local da máquina; com fuso, use por exemplo `2026-10-01T08:30:00-03:00`. Máquina e servidor Digifort devem usar o mesmo fuso para consultar gravações.

Um número JSON, **sem aspas**, representa dias de 24 horas antes da inicialização: `30` significa 30 dias atrás, `0.5` significa 12 horas atrás e `0` significa agora. Funciona tanto em `start_date` quanto em `end_date`. Os valores relativos são calculados uma única vez por execução, usando o mesmo instante para todas as câmeras; a janela não se desloca durante a coleta. Em uma nova execução, são recalculados, preservando a retomada do trabalho existente.

O fim é inclusivo para horários históricos: uma captura programada exatamente em `end_date` poderá ser salva. Nenhum arquivo novo recebe horário posterior ao fim. Um fim passado coleta somente o histórico; um fim futuro permite continuar ao vivo até esse limite. Cada câmera encerra independentemente; quando todas terminam, o programa encerra. **Sem fim configurado, continua até Ctrl+C.**

## Retomada: a última imagem tem prioridade

1. Havendo imagens na pasta, continua da última data/hora no nome + intervalo.
2. Sem imagens, usa `start_date`, se informado.
3. Sem imagens e sem início, busca a gravação mais antiga (`history=true`) ou começa ao vivo agora (`history=false`).

**Imagens existentes têm prioridade sobre o início, inclusive “30 dias atrás”.** Para coletar novamente um período anterior, use outra pasta vazia. O fim sempre é respeitado, inclusive quando a última imagem já está além dele. Arquivos `.part` e nomes sem data válida são ignorados.

Cada pasta guarda `.history.json`, atualizado por renomeação, com o próximo horário do histórico. Um progresso válido mais adiante que a última imagem é preservado para não repetir lacunas já verificadas. Sem esse JSON ou com ele inválido, continua considerando a última imagem.

Ao reiniciar com `history=true`, recupera as gravações da parada antes de voltar ao vivo, desde que ainda existam no servidor e estejam dentro do fim configurado. Com `history=false`, uma captura atrasada ocorre imediatamente, sem recuperar o passado.

## Histórico e falhas

O histórico requer uma URL Digifort `/Interface/Cameras/GetSnapshot?Camera=...` e permissão para consultar e visualizar gravações. URLs comuns como `http://CAMERA/image.jpg` usam `history=false` e fornecem apenas imagens atuais. Uma data passada não transforma um endpoint ao vivo em histórico.

Com início explícito, busca períodos gravados em janelas de seis horas (`history_search_hours`). São seis horas **no calendário do histórico**, sem esperar seis horas entre consultas. A timeline permite encontrar gravações curtas dentro dessas janelas. Sem início, lê a timeline desde 1970 por streaming e confirma o período anterior ao primeiro trecho recebido para achar a data mais antiga, limitando a consulta pelo fim quando informado.

Após encontrar gravações, solicita imagens a cada `interval_seconds` no histórico. Os downloads são sequenciais por câmera, sem aguardar esse intervalo no relógio real. Exemplo com intervalo 60: solicita 08:00, 08:01, 08:02, até alcançar o limite.

Imagem ausente (código 10200) ou erro de decodificação Digifort (10201) avança somente um intervalo. Erros de rede, timeout, permissão ou disco repetem o mesmo ponto após `history_retry_seconds`. As outras câmeras continuam trabalhando. Erros de consulta não são tratados como ausência de gravação. A retenção depende do servidor; o coletor não presume que exista um mês completo.

## Campos de configuração

| Campo por câmera | Padrão / comportamento |
|---|---|
| `url` | Obrigatório, HTTP/HTTPS. |
| `folder` | Obrigatório e exclusivo; letras ASCII, números, `_` e `-`. |
| `interval_seconds` | Obrigatório, positivo; no histórico, mínimo de 0.001 s. |
| `history` | `false`; habilite para gravações Digifort. |
| `start_date` / `end_date` | Data/hora ISO, número de dias atrás ou `null`. |
| `capture_resolution` | Digifort ao vivo: `[1920, 1080]`; histórico: original se omitido. |
| `quality` | `100`, inteiro de 1 a 100. |
| `resize` | `null`: preserva os bytes; `[1280, 720]`: reduz no Python. |
| `history_search_hours` | `6`. |
| `history_retry_seconds` | `30`. |

O Digifort ao vivo usa `Profile=Recording` por padrão e preserva a proporção. Um `Profile` na URL é respeitado. Full HD é um padrão configurável, não a descoberta da resolução máxima. Se definir `capture_resolution`, substitui o tamanho da URL e também é usado no histórico. Se omitido, o playback solicita `Width=-1&Height=-1` para tamanho original. URLs de outros servidores não recebem parâmetros Digifort.

Sem `resize`, salva os bytes recebidos. Com ele, o Pillow reduz para caber nas dimensões, preservando proporção e sem ampliar imagens menores. JPEGs são recodificados com `quality`; PNGs mantêm compressão sem perdas. As respostas devem ter imagem não vazia e `Content-Type` JPEG, PNG, WebP, GIF, BMP ou TIFF.

Exemplo de arquivo: `volume/image/camera_01/2026-10-29_08-00-00.jpg`. Ao vivo, o nome usa o recebimento da imagem; no histórico, o horário solicitado ao Digifort, com precisão de milissegundos. Colisões usam microssegundos. Gravações usam temporários `.part`, renomeados depois de completos. Redimensionamento e gravação executam em uma thread. Execute uma instância por diretório de saída.

`OUTPUT_DIR` relativo é resolvido a partir da pasta do script. `REQUEST_TIMEOUT` limita cada requisição em segundos (padrão 10). Pastas reservadas do Windows, como `CON`, e nomes duplicados ignorando maiúsculas/minúsculas são rejeitados.

## Testes locais

```powershell
uv run python -m unittest discover -s tests -v
```