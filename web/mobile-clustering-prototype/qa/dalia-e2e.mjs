#!/usr/bin/env node
import fs from 'node:fs';
import puppeteer from 'puppeteer-core';

const base = process.argv[2] || 'http://127.0.0.1:5173';
const executablePath = process.env.CHROMIUM_PATH || ['/snap/bin/chromium', '/usr/bin/chromium', '/usr/bin/google-chrome'].find(fs.existsSync);
if (!executablePath) throw new Error('Chromium not found; set CHROMIUM_PATH');
const browser = await puppeteer.launch({headless: true, executablePath, args: ['--no-sandbox', '--disable-gpu']});
const page = await browser.newPage();
await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1, isMobile: true, hasTouch: true});
const errors = [];
page.on('console', (message) => { if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) errors.push(message.text()); });
page.on('pageerror', (error) => errors.push(error.message));
page.on('response', (response) => {
  if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`${response.status()} ${response.url()}`);
});

async function assertPage(condition, message) {
  if (!condition) throw new Error(message);
}

try {
  await page.goto(`${base}/?batch=dropbox-2025-03-19-web&backend=off&qa=puppeteer`, {waitUntil: 'networkidle0'});
  await assertPage(await page.$eval('.eyebrow', (el) => el.textContent.includes('2025-03-19')), 'current batch label missing');
  await assertPage(await page.$$eval('.batch-row', (rows) => rows.length === 18), 'expected 18 open batch rows');
  await assertPage(await page.$eval('body', (body) => body.scrollWidth <= document.documentElement.clientWidth), 'start screen horizontal overflow');
  await page.click('.start-btn');
  await page.waitForSelector('.group-card .identity-actions', {timeout: 5000});
  await assertPage(await page.$$eval('.identity-actions .identity-choice', (nodes) => nodes.map((node) => node.textContent.trim()).join('|') === 'מוצר קיים|מוצר חדש|לא בטוחה'), 'primary identity choices are not the required three');
  await page.waitForFunction(() => {
    const img = document.querySelector('.review-thumbs img');
    return img?.complete && img.naturalWidth > 0;
  }, {timeout: 10000});
  await assertPage(await page.$eval('.review-thumbs img', (img) => getComputedStyle(img).objectFit === 'contain'), 'review image is cropped instead of contained');
  await assertPage(await page.$eval('body', (body) => body.scrollWidth <= document.documentElement.clientWidth), 'review screen horizontal overflow');
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await new Promise((resolve) => setTimeout(resolve, 200));
  const floating = await page.evaluate(() => {
    const source = document.querySelector('[data-current-photo-block="true"]');
    return {
      scrollY: window.scrollY,
      maxScroll: Math.max(0, document.documentElement.scrollHeight - window.innerHeight),
      sourceTop: source?.getBoundingClientRect().top ?? null,
      floating: document.querySelector('[data-floating-current-photo="true"]')?.classList.contains('is-visible'),
      sourceHidden: source?.classList.contains('floating-source-hidden'),
      oldMiniStripCount: document.querySelectorAll('.sticky-current-photo').length,
    };
  });
  const crossedPinThreshold = floating.scrollY > 260 && floating.sourceTop < -20;
  await assertPage(floating.oldMiniStripCount === 0, `old duplicate mini strip returned: ${JSON.stringify(floating)}`);
  if (crossedPinThreshold) await assertPage(floating.floating && floating.sourceHidden, `single scrolling source-image invariant failed: ${JSON.stringify(floating)}`);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click('.identity-choice.existing');
  await page.waitForSelector('.existing_product_selection, .unified-match-flow', {timeout: 5000}).catch(() => {});
  await assertPage(await page.$eval('.unified-match-flow', (el) => Boolean(el)), 'existing-product finder did not open');
  await assertPage(await page.$$eval('.catalog-type-filter button', (nodes) => nodes.length >= 5), 'jewelry type picker missing');
  await assertPage(await page.$eval('.catalog-type-filter button.active', (node) => node.textContent.includes('טבעת')), 'historical candidate type did not activate the ring filter');
  await assertPage(await page.$eval('.suggestion-note', (node) => node.textContent.includes('מוצר קיים שכדאי להשוות')), 'historical suggestion is not labeled for manual comparison');
  await assertPage(await page.$$eval('.match-list-head strong', (nodes) => !nodes.some((node) => node.textContent.includes('עוד אפשרויות מהקטלוג'))), 'unrequested catalog alternatives are visible');
  await page.evaluate(() => window.scrollTo(0, 1100));
  await new Promise((resolve) => setTimeout(resolve, 250));
  const comparisonFloating = await page.evaluate(() => ({
    scrollY: window.scrollY,
    floating: document.querySelector('[data-floating-current-photo="true"]')?.classList.contains('is-visible'),
    sourceHidden: document.querySelector('[data-current-photo-block="true"]')?.classList.contains('floating-source-hidden'),
    visibleSourceContexts: [...document.querySelectorAll('[data-floating-current-photo="true"], [data-current-photo-block="true"]')].filter((el) => {
      const style = getComputedStyle(el);
      return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }).length,
  }));
  await assertPage(comparisonFloating.scrollY > 260 && comparisonFloating.floating && comparisonFloating.sourceHidden && comparisonFloating.visibleSourceContexts === 1, `comparison source context did not shrink to one floating image: ${JSON.stringify(comparisonFloating)}`);
  fs.mkdirSync('/home/server/Pictures/hermes-qa', {recursive: true});
  await page.screenshot({path: '/home/server/Pictures/hermes-qa/dalia-existing-product-390x844.png', fullPage: true});

  await page.goto(`${base}/?batch=dropbox-2026-06-29-web&backend=off&qa=puppeteer`, {waitUntil: 'networkidle0'});
  await page.click('.start-btn');
  await page.waitForSelector('.group-card .identity-actions', {timeout: 5000});
  await page.waitForFunction(() => {
    const img = document.querySelector('.review-thumbs img');
    return img?.complete && img.naturalWidth > 0;
  }, {timeout: 10000});
  await assertPage(await page.$eval('.review-context', (el) => el.textContent.includes('1 מתוך 8')), 'newest batch progress mismatch');
  await assertPage(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
  console.log(JSON.stringify({valid: true, viewport: '390x844', batch_rows: 18, tested_batches: ['dropbox-2025-03-19-web', 'dropbox-2026-06-29-web'], short_page_source_image: floating, comparison_source_image: comparisonFloating, browser_errors: errors, no_live_writes: true}, null, 2));
} finally {
  await browser.close();
}
