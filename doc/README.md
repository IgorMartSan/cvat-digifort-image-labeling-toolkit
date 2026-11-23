# Documentação de Referência do Sistema

Esta pasta contém documentações técnicas e manuais oficiais dos sistemas integrados, servindo como base de consulta para desenvolvedores e agentes autônomos.

---

## 📁 `doc/digifort/` — Documentação Oficial do Digifort VMS

Documentação técnica oficial fornecida pela Digifort cobrindo APIs HTTP, interface RTSP, playback e controle PTZ:

| Arquivo | Descrição | Principais Casos de Uso |
|---|---|---|
| [`Digifort HTTP API.pdf`](digifort/Digifort%20HTTP%20API.pdf) | Manual completo da API HTTP REST do Digifort | Endpoints de snapshot (`/Interface/Cameras/GetSnapshot`), telemetria PTZ, presets e autenticação |
| [`Digifort RTSP Interface 7.3.0.pdf`](digifort/Digifort%20RTSP%20Interface%207.3.0.pdf) | Especificação técnica de streams RTSP do servidor Digifort | Formato de URLs RTSP ao vivo (`/Interface/Cameras/Media?Camera=...`) e parâmetros de transporte |
| [`XDigiPlaybackCore.pdf`](digifort/XDigiPlaybackCore.pdf) | Controle de playback e busca em gravações históricas | Consulta a timelines gravadas, busca de data/hora inicial e avanço de vídeo |
| [`XDigiLiveCore.pdf`](digifort/XDigiLiveCore.pdf) | Biblioteca de controle de vídeo ao vivo | Conexão com streams ao vivo e gerenciamento de buffers |
| [`EventVariables.pdf`](digifort/EventVariables.pdf) | Dicionário de variáveis de eventos e metadados | Tratamento de eventos de alarme, metadados e tags de câmeras |
| [`SDK - Readme.pdf`](digifort/SDK%20-%20Readme.pdf) | Visão geral da arquitetura do SDK Digifort | Requisitos de integração e convenções gerais de código |
| [`ActiveX for HTML pages.pdf`](digifort/ActiveX%20for%20HTML%20pages.pdf) | Referência histórica de integração HTML | Parâmetros de exibição de câmeras e controles |
