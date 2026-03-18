/**
 * CV Renderer — Golden Template → PDF
 * 
 * Usage:
 *   node render_cv.js <data.json> [output.pdf]
 * 
 * The data.json file should contain all CV content.
 * The renderer injects it into the HTML template and 
 * uses Playwright Chromium to produce a pixel-perfect PDF.
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const TEMPLATE_PATH = path.join(__dirname, 'cv_template_modern_clean.html');

function buildHTML(template, data) {
  let html = template;

  // ── Simple tokens ──
  html = html.replace(/\{\{FULL_NAME\}\}/g, data.full_name || '');
  html = html.replace(/\{\{CONTACT_LINE\}\}/g, data.contact_line || '');
  html = html.replace(/\{\{PROFILE_TEXT\}\}/g, data.profile_text || '');

  // ── Education block ──
  const eduHTML = (data.education || []).map(edu => {
    let block = `
    <div class="edu-entry">
      <div class="edu-row">
        <div class="edu-row__degree">${edu.degree}</div>
        <div class="edu-row__dates">${edu.dates}</div>
      </div>
      <div class="edu-school">${edu.school}</div>`;
    if (edu.detail) {
      block += `\n      <div class="edu-detail">${edu.detail}</div>`;
    }
    block += `\n    </div>`;
    return block;
  }).join('\n');
  // Replace the education mustache block
  html = html.replace(/\{\{#EDUCATION\}\}[\s\S]*?\{\{\/EDUCATION\}\}/g, eduHTML);

  // ── Roles block ──
  const rolesHTML = (data.roles || []).map(role => {
    const bullets = (role.bullets || []).map(b => `      <li>${b}</li>`).join('\n');
    return `
    <div class="role">
      <div class="role__header">
        <div class="role__company">${role.company}</div>
        <div class="role__dates">${role.dates}</div>
      </div>
      <div class="role__title">${role.title} — ${role.location}</div>
      <ul class="role__bullets">
${bullets}
      </ul>
    </div>`;
  }).join('\n');
  html = html.replace(/\{\{#ROLES\}\}[\s\S]*?\{\{\/ROLES\}\}/g, rolesHTML);

  // ── Extras block ──
  if (data.extras && data.extras.length > 0) {
    const extrasHTML = data.extras.map(e => `    <li>${e}</li>`).join('\n');
    html = html.replace(
      /\{\{#HAS_EXTRAS\}\}[\s\S]*?\{\{\/HAS_EXTRAS\}\}/g,
      `<div class="section-title">Leadership &amp; Extra-Curricular</div>
<hr class="section-line">
<ul class="extras" style="list-style:none;padding:0;">
${extrasHTML}
</ul>`
    );
  } else {
    html = html.replace(/\{\{#HAS_EXTRAS\}\}[\s\S]*?\{\{\/HAS_EXTRAS\}\}/g, '');
  }

  // ── Skills ──
  html = html.replace(/\{\{SKILLS_HTML\}\}/g, data.skills_html || '');

  return html;
}

async function renderPDF(htmlContent, outputPath) {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  await page.setContent(htmlContent, { waitUntil: 'networkidle' });

  await page.pdf({
    path: outputPath,
    format: 'A4',
    margin: {
      top: '20mm',
      right: '22mm',
      bottom: '16mm',
      left: '22mm',
    },
    scale: 0.90,
    printBackground: true,
    preferCSSPageSize: false,
  });

  await browser.close();
  return outputPath;
}

async function main() {
  const dataPath = process.argv[2];
  const outputPath = process.argv[3] || 'output_cv.pdf';

  if (!dataPath) {
    console.error('Usage: node render_cv.js <data.json> [output.pdf]');
    process.exit(1);
  }

  // Load template
  const template = fs.readFileSync(TEMPLATE_PATH, 'utf-8');

  // Load data
  const data = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));

  // Build HTML
  const html = buildHTML(template, data);

  // Optional: save intermediate HTML for debugging
  const debugHTMLPath = outputPath.replace('.pdf', '_debug.html');
  fs.writeFileSync(debugHTMLPath, html, 'utf-8');
  console.log(`Debug HTML saved: ${debugHTMLPath}`);

  // Render PDF
  await renderPDF(html, outputPath);
  console.log(`PDF rendered: ${outputPath}`);

  // ── Validation ──
  const stats = fs.statSync(outputPath);
  const sizeKB = (stats.size / 1024).toFixed(1);
  console.log(`File size: ${sizeKB} KB`);
  if (stats.size < 5000) {
    console.warn('⚠ WARNING: PDF is suspiciously small — check for rendering issues');
  }
  console.log('✓ Done');
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
