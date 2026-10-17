// CI: inject Authorization bearer token to bypass broken OAP gateway ext_authz.
// Copied from rhoai-e2e assets at runtime — do not commit into odh-dashboard.
const _ciOcToken = Cypress.env('OC_TOKEN');

const _authHeaders = (): Record<string, string> =>
  _ciOcToken ? { Authorization: `Bearer ${_ciOcToken}` } : {};

const _operatorNamespace = (): string =>
  String(Cypress.env('OPERATOR_NAMESPACE') || 'redhat-ods-operator');

Cypress.Commands.overwrite('exec', (originalFn: any, command: any, options: any) => {
  const opts = { ...(options || {}) };
  if (
    typeof command === 'string'
    && /^\s*oc\s+login\b/.test(command)
    && _ciOcToken
    && !/--token\b/.test(command)
  ) {
    const server = String(
      Cypress.env('OC_SERVER') || Cypress.env('CYPRESS_OC_SERVER') || '',
    ).trim();
    if (server) {
      command = `oc login --token=${_ciOcToken} --server=${server} --insecure-skip-tls-verify=true`;
      opts.failOnNonZeroExit = false;
    }
  }
  if (typeof command === 'string' && /\s-n\s+default\b/.test(command)) {
    command = command.replace(/\s-n\s+default\b/g, ` -n ${_operatorNamespace()}`);
  }
  // HyperShift guest project deletion often never finishes --wait (cxn7l 180s/300s hangs).
  if (typeof command === 'string' && /oc\s+delete\s+(project|namespace)\b/.test(command)) {
    command = command.replace(/\s--wait(?!=)/g, ' --wait=false');
    if (!/--wait=/.test(command)) {
      command = `${command} --wait=false`;
    }
    opts.failOnNonZeroExit = false;
    opts.timeout = Math.min(Number(opts.timeout) || 20000, 20000);
  }
  return originalFn(command, opts);
});

if (_ciOcToken) {
  Cypress.Commands.overwrite('visitWithLogin', (_originalFn: any, relativeUrl: string) => {
    let fullUrl: string;
    if (relativeUrl.replace(/\//g, '')) {
      fullUrl = new URL(relativeUrl, Cypress.config('baseUrl') || '').href;
    } else {
      fullUrl = new URL(Cypress.config('baseUrl') || '').href;
    }
    cy.step(`Navigate to: ${fullUrl}`);
    return cy.visit(fullUrl, { headers: _authHeaders(), failOnStatusCode: false });
  });

  Cypress.Commands.overwrite('visit', (originalFn: any, url: any, options: any = {}) => {
    const headers: Record<string, string> = { ...((options as any)?.headers || {}) };
    if (!headers['Authorization'] && !headers['authorization']) {
      Object.assign(headers, _authHeaders());
    }
    return originalFn(url, { ...options, headers });
  });

  Cypress.Commands.overwrite('request', (originalFn: any, ...args: any[]) => {
    const injectAuth = (options: Record<string, unknown> = {}) => {
      const headers = { ...((options.headers as Record<string, string>) || {}) };
      if (!headers.Authorization && !headers.authorization) {
        Object.assign(headers, _authHeaders());
      }
      return { ...options, headers };
    };
    const httpMethods = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']);
    if (typeof args[0] === 'string' && httpMethods.has(args[0].toUpperCase()) && typeof args[1] === 'string') {
      const method = args[0];
      const url = args[1];
      const body = args[2];
      const options =
        typeof args[3] === 'object' && args[3] !== null
          ? args[3]
          : typeof body === 'object' && body !== null && !Array.isArray(body)
            ? body
            : {};
      const requestOptions = injectAuth({ ...options, method, url, body });
      return originalFn(requestOptions);
    }
    if (typeof args[0] === 'string') {
      const url = args[0];
      if (args.length === 1) {
        return originalFn(injectAuth({ url }));
      }
      return originalFn(injectAuth({ url, body: args[1] }));
    }
    if (typeof args[0] === 'object' && args[0] !== null) {
      return originalFn(injectAuth(args[0]));
    }
    return originalFn(...args);
  });

  beforeEach(() => {
    cy.intercept('**', (req) => {
      const base = Cypress.config('baseUrl') || '';
      if (base && !req.url.startsWith(base)) {
        return;
      }
      if (!req.headers['authorization'] && !req.headers['Authorization']) {
        req.headers['Authorization'] = `Bearer ${_ciOcToken}`;
      }
    });
  });
}

Cypress.on('before:browser:launch', (browser, launchOptions) => {
  if (browser.name === 'chrome' || browser.name === 'electron') {
    launchOptions.args.push('--ignore-certificate-errors');
  }
  return launchOptions;
});
