# Instruções para configurar câmeras, Digifort e CVAT

## Objetivo

Quando o usuário enviar links de câmeras, do Digifort ou do CVAT, transformar
essas informações em configurações funcionais dos scripts deste repositório.
O usuário espera que o agente **pergunte o que falta**, configure os arquivos e
explique como iniciar. Não se limite a entregar exemplos para ele adaptar.

Estas instruções de entrevista se aplicam quando houver uma solicitação de
configuração. Uma tarefa de documentação, por si só, não exige perguntar dados
de câmeras ou credenciais.

## Primeiro, aproveitar o que já existe

1. Ler `README_USO.md`, os READMEs dos scripts e os `.env.example` pertinentes.
2. Consultar os `.env` existentes sem exibir senhas ou URLs com credenciais.
3. Extrair dos links as informações disponíveis: servidor, protocolo, câmera,
   porta, projeto e organização, quando identificáveis.
4. Reutilizar valores já fornecidos pelo usuário para o mesmo contexto. Não
   perguntar novamente algo que esteja claro na conversa ou na configuração.
5. Listar mentalmente os dados ausentes e fazer perguntas curtas e agrupadas.
   Continuar as verificações e preparações que não dependam das respostas.

Não inventar credenciais, IDs de projeto, nomes de câmera, datas de início ou
autorizações de remoção. Valores propostos devem ser apresentados como sugestões.

## Perguntas sobre as câmeras

Perguntar somente os itens que ainda não puderem ser determinados:

| Informação | Como obter ou perguntar |
|---|---|
| Câmeras | Extrair `Camera` da URL Digifort; se faltar, pedir os identificadores. |
| Servidor e acesso | Reutilizar a conexão existente; pedir o endereço/acesso que faltar. |
| Histórico ou ao vivo | Perguntar se deseja recuperar gravações ou capturar somente imagens atuais. |
| Início | Data e hora, número de dias atrás ou gravação mais antiga disponível. |
| Fim | Data e hora final, até o momento da inicialização ou coleta contínua. |
| Intervalo | Perguntar a frequência e converter minutos para `interval_seconds`. |
| Resolução | Perguntar a finalidade e se pode usar o perfil de resolução já escolhido. |
| Pasta de saída | Reutilizar `OUTPUT_DIR` quando adequado; confirmar outro destino se necessário. |

Uma pergunta agrupada possível, adaptada aos dados realmente ausentes:

> Para essas câmeras, deseja começar de quantos dias atrás, capturar a cada
> quantos minutos e continuar sem data final? Podemos manter a resolução e a
> pasta de saída atuais?

Uma URL RTSP não é uma URL de snapshot HTTP. O coletor atual usa HTTP/HTTPS.
Consultar a documentação local do Digifort para identificar um endpoint adequado;
se a porta/interface HTTP não estiver identificável, perguntar ou testar o
endpoint documentado no servidor informado. Não afirmar que a conversão funciona
sem verificar. Não implementar suporte RTSP novo quando bastar configurar o HTTP.

## Perguntas sobre o CVAT

| Informação | Como obter ou perguntar |
|---|---|
| Servidor | Extrair a origem do link CVAT ou reutilizar `CVAT_URL`. |
| Projeto | Por padrão, criar/reutilizar na organização um projeto com o nome da pasta. Não pedir ID nesse fluxo. Se o usuário indicar projeto fixo, extrair o ID de `/projects/ID`, sem confundir com ID de task. |
| Mapeamento | Confirmar as pastas e a organização. Perguntar destinos diferentes somente quando essa intenção estiver presente. |
| Acesso | Reutilizar credenciais locais; se faltarem ou forem recusadas, pedir atualização no `.env`. |
| Organização | Identificar o slug quando possível; perguntar se o contexto do projeto não estiver claro. |
| Labels | Para projetos novos, perguntar as classes que faltarem ou se deseja criar sem labels. Não inventar classes. Projetos existentes preservam seus labels. |
| Lote | Perguntar a quantidade de imagens por task, inclusive diferenças entre pastas. |
| Remoção | Perguntar se deve manter as imagens locais ou remover após confirmação do CVAT. |
| Operação | Reutilizar o modo contínuo e o intervalo de verificação previamente definidos, ou perguntar se ausentes. |

Exemplo de pergunta quando apenas esses dados estiverem faltando:

> Posso criar um projeto por câmera nessa organização? Quais classes os projetos
> devem ter, ou prefere criá-los sem labels? Quantas imagens por task e devo
> manter ou remover as imagens locais após confirmar o upload?

Se as credenciais estiverem ausentes, orientar o usuário a preenchê-las no `.env`;
não exigir que publique a senha no chat. Se o login falhar, informar o resultado
e pedir a correção necessária, sem repetir tentativas indefinidamente durante
uma tarefa de configuração.

## Arquivos que devem ser configurados

### Coletor: `digifort-scripts/.env`

- `CAMERAS` é uma lista JSON com um objeto por câmera.
- Cada objeto usa `url`, `folder`, `interval_seconds`, `history`, `start_date`,
  `end_date` e as opções de qualidade/resolução necessárias.
- `start_date` numérico representa dias de 24 horas antes da inicialização.
- `start_date: null` com histórico busca a gravação mais antiga disponível.
- `end_date: null` significa sem fim; `end_date: 0` encerra no momento de referência.
- Datas fixas usam ISO 8601, incluindo hora e, quando informado, fuso.
- Usar nomes de pasta válidos e exclusivos. Normalmente `folder` corresponde
  ao identificador da câmera fornecido pelo usuário.

### Uploader: `cvat_scripts/.env`

- Manter `CVAT_URL`, `CVAT_USERNAME` e `CVAT_PASSWORD` como conexão compartilhada.
- `FOLDERS` é uma lista JSON, não uma única pasta em variáveis soltas.
- Cada objeto usa `images_dir`, `images_per_task` e, opcionalmente,
  `auto_remove`, `organization` e `labels`.
- Sem `project_id`, buscar/criar na organização o projeto com o nome da pasta.
  `project_id` é opcional para indicar um projeto existente explicitamente.
- `CVAT_LABELS` é uma lista JSON de classes para projetos novos; `[]` cria sem
  labels quando essa for a escolha do usuário. Não alterar labels já existentes.
- O nome final da pasta em `images_dir` é o nome da câmera no CVAT.
- Projetos, tamanhos de lote e remoção podem variar entre objetos.
- `LOOP`, `POLL_INTERVAL_SECONDS`, `DRY_RUN`, `AUTO_REMOVE` e
  `CVAT_ORGANIZATION` são opções globais, conforme o código e os exemplos atuais.
- Cada pasta tem seu próprio ponto de retomada no projeto escolhido.

Resolver os caminhos reais: `images_dir` deve corresponder exatamente a
`OUTPUT_DIR / folder`, mesmo se `OUTPUT_DIR` apontar para fora da pasta do coletor.
Não assumir o caminho padrão sem ler a configuração.

### Orquestrador da Pipeline Unificada: `pipeline.env` e `pipeline.py`

- `pipeline.py` é o ponto de entrada unificado que junta o coletor de câmeras e o
  uploader CVAT em um motor assíncrono único e contínuo, sem necessidade de processos
  separados ou terminais múltiplos.
- `PIPELINE` em `pipeline.env` referencia os caminhos dos `.env` de cada módulo:
  - `cameras.env_file`: caminho para `digifort-scripts/.env` (obrigatório).
  - `cvat.env_file` e `cvat.enabled`: referência para `cvat_scripts/.env` (opcional).
  - `RESTART_SECONDS`: intervalo padrão de reconexão (padrão **13 segundos**).
- **Regra de Retry Infinito a cada 13 segundos (nunca interrompe a pipeline):**
  - **Por câmera:** cada câmera tem seu próprio worker independente. Se a conexão cair,
    o snapshot falhar ou a câmera ficar offline, o worker emite um aviso e
    **tenta reconectar a cada 13 segundos infinitamente**. Câmeras saudáveis continuam
    capturando normalmente e a pipeline nunca para.
  - **Para o CVAT:** o worker do uploader executa em paralelo. Se o servidor CVAT estiver
    fora do ar, a rede cair ou o login falhar, o worker do CVAT emite um aviso e
    **tenta reconectar a cada 13 segundos infinitamente**. A coleta de fotos no disco
    continua ativa sem qualquer bloqueio.
  - **Tolerância a ausência:** se o CVAT não for configurado ou estiver desabilitado,
    a pipeline roda exclusivamente a coleta contínua das câmeras.
  - **Backlog e Retomada:** enquanto o CVAT estiver fora, as imagens se acumulam com
    segurança no volume local (com checkpoint em `.history.json`). Assim que o CVAT
    reconectar (ou ao reiniciar com credenciais válidas), o uploader detecta todo o
    backlog de imagens pendentes, monta os lotes completos e envia em ordem cronológica.
- Comandos da pipeline:
  - `uv run python pipeline.py`: executa a pipeline unificada contínua.
  - `uv run python pipeline.py --check`: valida sintaxe, arquivos e JSONs sem disparar conexões.
  - `uv run python pipeline.py --collect-only`: executa exclusivamente as câmeras.
  - `uv run python pipeline.py --dry-run`: executa a simulação do CVAT sem criar tasks.
  - `uv run python pipeline.py --send`: força envio real ao CVAT (ignora `DRY_RUN=true`).

## Perfil já utilizado neste projeto

Estes são valores de referência previamente usados, não requisitos para todo
novo pedido. Preservar escolhas explícitas do usuário e perguntar o que faltar:

- Histórico de 30 dias, coleta a cada 600 segundos e sem data final.
- Imagens de até 1280×720, preservando proporção, com qualidade JPEG 95.
- Para proporção 4:3, o resultado pode ser 960×720; não distorcer para 16:9.
- Lotes de 50 imagens e verificação do uploader a cada 5 segundos.
- Remoção automática somente quando já definida ou solicitada pelo usuário.

Não prometer resolução ideal para qualquer treinamento YOLO; distinguir o
tamanho das imagens coletadas do `imgsz` escolhido no treinamento.

## Regras de preservação e retomada

- Manter dois scripts: coleta e envio. Um uploader processa todas as entradas
  de `FOLDERS`; não abrir um processo separado por pasta sem necessidade.
- A pipeline (`pipeline.py`) unifica a supervisão mantendo a separação dos processos,
  isolamento de dependências via uv e limpeza de variáveis de ambiente.
- Preservar câmeras, pastas e configurações não relacionadas ao pedido.
- Não remover imagens ou checkpoints para fazer uma configuração começar do zero.
  Se o usuário quiser reprocessar desde uma data anterior, explicar a prioridade
  da retomada e definir o destino apropriado com ele.
- Preservar `.history.json`: registra a pasta, a última imagem salva e o próximo
  horário, inclusive depois da remoção dos arquivos pelo uploader.
- O uploader retoma após a maior data final das tasks daquela câmera no projeto.
- Esperar lotes completos. Não enviar o restante incompleto automaticamente.
- Remover somente arquivos do lote cujo upload, quantidade e nomes foram
  confirmados. A simulação não remove arquivos.
- Uma falha de câmera não deve interromper as outras; uma falha em uma pasta de
  envio não deve impedir a verificação das demais; falha no CVAT não interrompe a coleta.

## Validar e concluir a configuração

1. Validar os JSONs, campos, caminhos, nomes de pasta e projetos por entrada.
2. Validar a integridade da pipeline com `uv run python pipeline.py --check`.
3. Se fizer sentido e houver acesso, testar snapshots em memória e consultar o
   destino CVAT em modo de leitura/simulação. Não salvar fotos atuais nas pastas
   de coleta apenas para testar: isso pode alterar o início da coleta histórica.
4. Distinguir configuração validada, câmera testada, login confirmado e upload
   realizado. Não afirmar que o envio foi testado usando apenas mocks ou simulação.
5. Para alterações de código, executar os testes pertinentes nas pastas afetadas:
   `uv run python -m unittest discover -s tests -v` (na raiz, em `digifort-scripts` e em `cvat_scripts`).
6. Atualizar exemplos e documentação quando mudar o formato de configuração.
   Usar placeholders nos arquivos versionáveis; manter credenciais nos `.env`.
7. Quando o pedido for apenas configurar, não iniciar processos infinitos ou
   enviar lotes automaticamente. Se o usuário já tiver pedido para executar,
   seguir essa autorização sem pedir a mesma confirmação novamente.

A resposta final deve resumir câmeras, período, intervalo, resolução, pastas,
projetos, lotes e remoção; explicar pendências reais de acesso e fornecer os
comandos para iniciar/reiniciar a pipeline ou os scripts individuais. Não reproduzir credenciais.

## Documentação de apoio

- [Guia de uso](README_USO.md).
- [Visão geral](README.md).
- [Orquestrador da Pipeline](pipeline/README.md) e [seu modelo](pipeline.env.example).
- [Coletor](digifort-scripts/README.md) e [seu modelo de configuração](digifort-scripts/.env.example).
- [Uploader](cvat_scripts/README.md) e [seu modelo de configuração](cvat_scripts/.env.example).
- [Documentação local do Digifort](digifort-scripts/digifort-doc/).
