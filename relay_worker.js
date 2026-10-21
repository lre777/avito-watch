/**
 * Релей для Telegram Bot API на Cloudflare Workers.
 *
 * Нужен, если провайдер блокирует api.telegram.org: скрипт обращается к этому
 * воркеру, а тот уже сам ходит в Telegram из сети Cloudflare.
 *
 * Как развернуть (5 минут, бесплатно):
 *   1. Зарегистрируйтесь на https://dash.cloudflare.com (нужна только почта).
 *   2. Workers & Pages → Create → Worker → назовите, например, tg-relay → Deploy.
 *   3. Edit code → вставьте этот файл целиком → Deploy.
 *   4. Settings → Variables and Secrets → Add:
 *        имя: RELAY_SECRET, значение: любая длинная строка (пароль релея)
 *   5. Скопируйте адрес вида https://tg-relay.ВАШ_АККАУНТ.workers.dev
 *   6. В avito/config.json:
 *        "telegram": { "api_base": "https://tg-relay.ВАШ_АККАУНТ.workers.dev",
 *                      "relay_secret": "та же строка" }
 *
 * Токен бота в релее не хранится: он приходит в пути запроса, как и в обычном
 * Bot API. Без правильного RELAY_SECRET воркер отвечает 403.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/bot')) {
      return new Response('not found', { status: 404 });
    }
    if (env.RELAY_SECRET && request.headers.get('x-relay-secret') !== env.RELAY_SECRET) {
      return new Response('forbidden', { status: 403 });
    }

    const target = 'https://api.telegram.org' + url.pathname + url.search;
    const init = {
      method: request.method,
      headers: {
        'content-type': request.headers.get('content-type') || 'application/x-www-form-urlencoded',
      },
    };
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      init.body = await request.text();
    }

    const resp = await fetch(target, init);
    return new Response(resp.body, {
      status: resp.status,
      headers: { 'content-type': 'application/json' },
    });
  },
};
