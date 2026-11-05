// SSE Client Handler for CVAT AI Toolkit

function initSSE(channel, options = {}) {
    const url = `/training/stream` ? (channel === 'training' ? '/training/stream' : '/prediction/stream') : '';
    const eventSource = new EventSource(url);
    const terminal = document.getElementById(options.terminalId || 'terminal-output');
    const progressBar = document.getElementById(options.progressBarId || 'progress-bar');
    const progressText = document.getElementById(options.progressTextId || 'progress-text');

    eventSource.onmessage = function (event) {
        try {
            const data = JSON.parse(event.data);
            
            // Log line formatting
            if (terminal) {
                const line = document.createElement('div');
                const timeSpan = `<span class="log-line-time">[${data.time || ''}]</span>`;

                if (data.type === 'epoch') {
                    line.className = 'log-line-epoch';
                    line.innerHTML = `${timeSpan} ${data.text}`;
                } else if (data.type === 'success') {
                    line.className = 'log-line-success';
                    line.innerHTML = `${timeSpan} ${data.text}`;
                } else if (data.type === 'error') {
                    line.className = 'log-line-error';
                    line.innerHTML = `${timeSpan} ${data.text}`;
                } else {
                    line.innerHTML = `${timeSpan} ${data.text || JSON.stringify(data)}`;
                }

                terminal.appendChild(line);
                const maxEntries = window.MAX_LOG_ENTRIES || 20;
                while (terminal.children.length > maxEntries) {
                    terminal.removeChild(terminal.firstChild);
                }
                terminal.scrollTop = terminal.scrollHeight;
            }

            // Progress bar updates
            if (progressBar && data.percent !== undefined) {
                progressBar.style.width = `${data.percent}%`;
                progressBar.setAttribute('aria-valuenow', data.percent);
            }

            if (progressText && data.percent !== undefined) {
                progressText.innerText = `${data.percent}%`;
            }

            // Custom event handler
            if (options.onEvent) {
                options.onEvent(data);
            }

        } catch (e) {
            console.error("Erro ao processar evento SSE:", e);
        }
    };

    eventSource.onerror = function () {
        console.warn(`Conexão SSE (${channel}) reconectando...`);
    };

    return eventSource;
}
