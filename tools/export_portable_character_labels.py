"""Project exact current-Master names for the public database's character IDs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from gkms_tool.portable_character_labels import SCHEMA,MASTER_HASH,MODEL_MANIFEST_SHA256
from gkms_tool.portable_model_assets import load_portable_model_assets

SOURCE_MANIFEST_SHA256='aeb99a1573e83cee243866f0ead382e5665e3e916826a3f0c2ec0db258afbca4'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def export(source_manifest,model_assets,output):
    import yaml
    source_manifest,model_assets,output=map(Path,(source_manifest,model_assets,output))
    if output.exists():raise ValueError('Use a new character-label export directory')
    source=json.loads(source_manifest.read_bytes())
    if sha(source_manifest.read_bytes())!=SOURCE_MANIFEST_SHA256 or source['master_hash']!=MASTER_HASH:
        raise ValueError('Current Character Master source differs')
    load_portable_model_assets(model_assets,expected_sha256=MODEL_MANIFEST_SHA256)
    path=Path(source['master_dir'])/'Character.yaml';raw=path.read_bytes()
    if sha(raw)!=source['yaml_sha256']['Character.yaml']:raise ValueError('Character table changed')
    rows=yaml.load(raw,Loader=yaml.CSafeLoader)
    values={}
    for row in rows:
        if any(not isinstance(row.get(key),str) or not row[key].strip() for key in ('id','lastName','firstName')) or row['id'] in values:
            raise ValueError('Character name is missing or ambiguous')
        values[row['id']]=row['lastName']+row['firstName']
    with sqlite3.connect((model_assets/'master.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as connection:
        ids=[row[0] for row in connection.execute('SELECT DISTINCT character_id FROM idol_card ORDER BY character_id')]
    if not set(ids)<=values.keys():raise ValueError('Public idol database has an unknown character')
    labels=encode({key:values[key] for key in ids})
    manifest={'schema':SCHEMA,'master_hash':MASTER_HASH,'model_manifest_sha256':MODEL_MANIFEST_SHA256,
        'source_manifest_sha256':SOURCE_MANIFEST_SHA256,'character_table_sha256':sha(raw),'upstream_commit':source['upstream_commit'],
        'character_count':len(ids),'labels':{'path':'characters.json','sha256':sha(labels),'bytes':len(labels)},
        'display_only':True,'inferred_from_ids':False,'name_rule':'exact Character.lastName + Character.firstName; existing installed translations may override display'}
    encoded=encode(manifest);output.mkdir(parents=True);(output/'manifest.json').write_bytes(encoded);(output/'characters.json').write_bytes(labels)
    (output/'NOTICE.txt').write_text('Display names projected from verified game Master Character at '+source['upstream_commit']+'.\n'
        'Only names for character IDs present in the unchanged public database are included. No account data is included.\n'
        'This provenance notice does not grant a separate license for upstream game data.\n',encoding='utf-8')
    return {'manifest_sha256':sha(encoded),'character_count':len(ids),'labels_sha256':sha(labels),'game_io':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-manifest','model-assets','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();print(json.dumps(export(args.source_manifest,args.model_assets,args.output)))
