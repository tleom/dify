FROM langgenius/dify-agent-local-sandbox@sha256:f4d404c9dcdf80bd72f65e84c3f2bbd1f3404fd4b363db837176da8aaf771b1d

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    CHROME_BIN=/usr/bin/google-chrome \
    MPLBACKEND=Agg

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg unzip zip jq ripgrep git file \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      poppler-utils fontconfig fonts-noto-cjk fonts-noto-color-emoji \
      fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
      pandoc ghostscript qpdf tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
      libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
      libasound2 libcups2 libgtk-3-0 \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && fc-cache -f && rm -rf /var/lib/apt/lists/*

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.ustc.edu.cn/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-draw python3-uno ffmpeg libimage-exiftool-perl p7zip-full unrar-free gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.in requirements.lock.txt /opt/office/
RUN /usr/local/bin/python -m venv /opt/office/python \
    && /opt/office/python/bin/python -m pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r /opt/office/requirements.lock.txt \
    && /opt/office/python/bin/python -m pip check \
    && /opt/office/python/bin/python -m pip freeze --all > /opt/office/requirements.lock.txt

COPY package.json package-lock.json /opt/office/node/
RUN npm ci --prefix /opt/office/node --no-fund --no-audit \
    && npm ls --prefix /opt/office/node --all \
    && npm cache clean --force

COPY RUNTIME.md /opt/office/README.md
COPY smoke.py smoke.cjs /opt/office/
COPY office.py uno_worker.py /opt/office/
COPY fonts/ /usr/local/share/fonts/workbench/
RUN fc-cache -f
RUN google-chrome --version > /opt/office/chrome-version.txt \
    && dpkg-query -W > /opt/office/system-packages.txt

ENV PATH=/opt/user-env/current/python/bin:/opt/office/python/bin:/opt/user-env/current/node/node_modules/.bin:/opt/office/node/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
    NODE_PATH=/opt/user-env/current/node/node_modules:/opt/office/node/node_modules
USER dify
