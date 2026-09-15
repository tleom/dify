#!/bin/sh
exec node /opt/office/mermaid/node_modules/@mermaid-js/mermaid-cli/src/cli.js --puppeteerConfigFile /opt/office/puppeteer-config.json "$@"
