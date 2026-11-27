// Сводка по Директу в Telegram: расход, показы, клики, цена клика, заявки с сайта. Запуск: node direct-report.mjs [--dry]
import { chromium } from "/home/void/.config/opencode/node_modules/playwright-core/index.mjs";
import fs from "node:fs";
import { execFileSync, execSync } from "node:child_process";
import os from "node:os";

const DRY = process.argv.includes("--dry");
const home = os.homedir();
const send = (t) => { if (DRY) console.log(t); else execFileSync("node", [`${home}/.config/opencode/tg-bot/notify.mjs`, t]); };

function ensureBrowser() {
  try { execSync("curl -s -m 4 http://127.0.0.1:9336/json/version", { stdio: "ignore" }); return; } catch {}
  try { execSync("ss -ltn | grep -q 11080 || ssh -f -N -o ExitOnForwardFailure=yes -i ~/.ssh/id_router -D 127.0.0.1:11080 root@192.168.1.1", { shell: "/bin/bash" }); } catch {}
  execSync(`cd /tmp/opencode && (setsid nohup chromium --headless=new --remote-debugging-port=9336 --user-data-dir=/tmp/opencode/ya-profile --proxy-server=socks5://127.0.0.1:11080 --no-first-run --disable-gpu >/tmp/opencode/ya-chrome.log 2>&1 </dev/null &) ; sleep 8`, { shell: "/bin/bash" });
}

function leadsToday() {
  try {
    const day = new Date().toISOString().slice(0, 10);
    return fs.readFileSync(`${home}/WORKSPACE/logs/leads.jsonl`, "utf8").split("\n").filter((l) => l.includes(`"at": "${day}`) && !/Проверка|тест/i.test(l)).length;
  } catch { return 0; }
}

try {
  ensureBrowser();
  const b = await chromium.connectOverCDP("http://127.0.0.1:9336", { timeout: 30000 });
  const p = await b.contexts()[0].newPage();
  await p.goto("https://direct.yandex.ru/dna/grid/campaigns?ulogin=beslanovich-void&stat-preset=last30Days", { waitUntil: "commit", timeout: 60000 });
  await p.waitForTimeout(15000);
  const t = await p.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
  await p.close();
  const bal = (t.match(/beslanovich-void ([\d\s]+,\d\d) ₽/) || [])[1] || "?";
  const i = t.indexOf("№ 7");
  const row = t.slice(i, i + 400);
  const m = row.match(/(Приостановлена|На модерации|Идут показы|Остановлена|Показы начнутся[^П]*?)\s*[\d\s]+,\d\d ₽ в неделю.*?Поиск ([\d\s]+,\d\d) (\d[\d\s]*?) (\d+) (\d+)/);
  const status = (row.match(/(Показы начнутся по расписанию[^П]*?|Идут показы|На модерации|Приостановлена|Остановлена|Отклонена)/) || [])[1] || "?";
  let msg = `📊 Директ · ремонт СПб\nСтатус: ${status.trim()}\nБаланс: ${bal} ₽`;
  if (m) {
    const spend = parseFloat(m[2].replace(/\s/g, "").replace(",", "."));
    const shows = m[3].replace(/\s/g, ""), clicks = parseInt(m[4]), conv = m[5];
    msg += `\nРасход: ${spend.toFixed(0)} ₽ · показы ${shows} · клики ${clicks}` + (clicks ? ` · клик ≈ ${(spend / clicks).toFixed(0)} ₽` : "");
    msg += `\nКонверсии Директа: ${conv}`;
    if (clicks >= 100 && !+conv && leadsToday() === 0) msg += `\n⚠️ 100+ кликов без заявок: останавливаем и разбираем запросы`;
  }
  msg += `\nЗаявок с сайта сегодня: ${leadsToday()}`;
  send(msg);
} catch (e) {
  send(`📊 Директ: сводка не собралась (${String(e).slice(0, 100)}). Проверю браузер.`);
}
process.exit(0);
