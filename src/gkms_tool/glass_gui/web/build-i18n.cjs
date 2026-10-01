'use strict';
// Run with Node after editing i18n.json. No network, game or host is involved.
const fs = require('node:fs');
const path = require('node:path');
const source = JSON.parse(fs.readFileSync(path.join(__dirname, 'i18n.json'), 'utf8'));
const languages = ['zh-Hant', 'en', 'ja'];
const expected = Object.keys(source[languages[0]]).sort().join('\n');
for (const language of languages) {
  if (Object.keys(source[language] || {}).sort().join('\n') !== expected)
    throw new Error('Translation key mismatch: ' + language);
}
fs.writeFileSync(path.join(__dirname, 'i18n.js'),
  "// Generated from i18n.json by build-i18n.cjs; edit the JSON source.\n" +
  "'use strict';\nwindow.GKMS_TRANSLATIONS = " + JSON.stringify(source, null, 2).replace(/</g, '\\u003c') + ';\n');
