import os, re, json, uuid, base64, shutil, threading, subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
SERVICE_NAME = 'HTML to APK Builder API'
SERVICE_VERSION = '1.2.0'
SDK = os.environ.get('ANDROID_SDK_ROOT', '/opt/android-sdk')
DATA = Path(os.environ.get('BUILD_DATA_DIR', '/tmp/html-apk-builder'))
JOBS_DIR, APK_DIR, WORK_DIR = DATA/'jobs', DATA/'apks', DATA/'work'
for d in (JOBS_DIR, APK_DIR, WORK_DIR): d.mkdir(parents=True, exist_ok=True)
JOBS, LOCK = {}, threading.Lock()
PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')
VERSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,49}$')
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, BUILD_TIMEOUT = 500, 10*1024*1024, 40*1024*1024, 600

def now(): return datetime.now(timezone.utc).isoformat()
def err(message, status=400): return jsonify({'success':False,'error':message,'time':now()}), status
def save_job(job):
    p=JOBS_DIR/(job['build_id']+'.json'); t=p.with_suffix('.tmp')
    t.write_text(json.dumps(job), encoding='utf-8'); t.replace(p)
def get_job(build_id):
    with LOCK:
        if build_id in JOBS: return dict(JOBS[build_id])
    p=JOBS_DIR/(build_id+'.json')
    if not p.is_file(): return None
    try: job=json.loads(p.read_text(encoding='utf-8'))
    except Exception: return None
    with LOCK: JOBS[build_id]=job
    return dict(job)
def update_job(build_id, **fields):
    with LOCK:
        job=JOBS.get(build_id)
        if job is None: return
        job.update(fields); job['updated_at']=now(); snap=dict(job)
    save_job(snap)
def safe_path(raw):
    if not isinstance(raw,str): raise ValueError('Every file must have a string path.')
    value=raw.replace('\\','/').strip(); parts=value.split('/')
    if not value or value.startswith('/') or re.match(r'^[A-Za-z]:',value) or any(x in ('','.','..') for x in parts) or ':' in value or '\x00' in value:
        raise ValueError('Invalid or unsafe file path: '+value)
    if value in ('project.json','draft.html'): raise ValueError('Reserved project metadata cannot be uploaded.')
    return PurePosixPath(value)
def receive_files(data, dest):
    entries=data.get('files')
    if not isinstance(entries,list) or not entries: raise ValueError("HTML projects require a 'files' array with path and content_base64.")
    if len(entries)>MAX_FILES: raise ValueError('Too many files; maximum is 500.')
    seen=set(); total=0; dest.mkdir(parents=True,exist_ok=True)
    for entry in entries:
        if not isinstance(entry,dict): raise ValueError('Each file entry must be an object.')
        rel=safe_path(entry.get('path')); name=rel.as_posix()
        if name in seen: raise ValueError('Duplicate file path: '+name)
        seen.add(name); encoded=entry.get('content_base64')
        if not isinstance(encoded,str): raise ValueError('Missing content_base64 for '+name)
        try: content=base64.b64decode(encoded,validate=True)
        except Exception: raise ValueError('Invalid base64 content for '+name)
        if len(content)>MAX_FILE_BYTES: raise ValueError('File exceeds 10 MB: '+name)
        total+=len(content)
        if total>MAX_TOTAL_BYTES: raise ValueError('Total project files exceed 40 MB.')
        out=dest.joinpath(*rel.parts); out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(content)
    if not (dest/'index.html').is_file(): raise ValueError('Project must contain index.html.')
    return len(entries),total
def make_project(project, uploaded, data):
    name=str(data.get('app_name','HTML App')).strip()[:80] or 'HTML App'
    package=data.get('package_name','com.generated.htmlapp')
    if not isinstance(package,str) or not PACKAGE_RE.fullmatch(package): raise ValueError('Invalid package_name; example: com.example.myapp')
    version=str(data.get('version_name','1.0'))
    if not VERSION_RE.fullmatch(version): raise ValueError('Invalid version_name.')
    try: version_code=int(data.get('version_code',1))
    except Exception: raise ValueError('version_code must be an integer.')
    if version_code<1 or version_code>2100000000: raise ValueError('version_code is out of range.')
    source_type=data.get('source_type','html'); url=str(data.get('website_url','')).strip()
    if source_type not in ('html','url'): raise ValueError("source_type must be 'html' or 'url'.")
    if source_type=='url' and not url.startswith(('https://','http://')): raise ValueError('A valid website_url is required.')
    appdir=project/'app'; main=appdir/'src'/'main'; assets=main/'assets'/'www'; javadir=main/'java'
    for part in package.split('.'): javadir=javadir/part
    assets.mkdir(parents=True,exist_ok=True); javadir.mkdir(parents=True,exist_ok=True)
    if source_type=='html':
        for f in uploaded.rglob('*'):
            if f.is_file():
                target=assets/f.relative_to(uploaded); target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(f,target)
        start='file:///android_asset/www/index.html'
    else: start=url.replace('\\','\\\\').replace('"','\\"')
    (project/'settings.gradle').write_text("pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = 'HtmlGeneratedApp'\ninclude ':app'\n",encoding='utf-8')
    (project/'build.gradle').write_text("plugins { id 'com.android.application' version '8.7.3' apply false }\n",encoding='utf-8')
    (project/'gradle.properties').write_text('org.gradle.jvmargs=-Xmx1536m -Dfile.encoding=UTF-8\norg.gradle.parallel=false\nandroid.useAndroidX=true\n',encoding='utf-8')
    (appdir/'build.gradle').write_text("plugins { id 'com.android.application' }\nandroid {\n namespace '"+package+"'\n compileSdk 35\n defaultConfig {\n applicationId '"+package+"'\n minSdk 23\n targetSdk 28\n versionCode "+str(version_code)+"\n versionName '"+version+"'\n }\n}\n",encoding='utf-8')
    java=('package '+package+';\nimport android.app.Activity;\nimport android.os.Bundle;\nimport android.webkit.WebSettings;\nimport android.webkit.WebView;\nimport android.webkit.WebViewClient;\nimport android.webkit.WebChromeClient;\npublic class MainActivity extends Activity { private WebView webView; @Override public void onCreate(Bundle b) { super.onCreate(b); webView=new WebView(this); setContentView(webView); WebSettings s=webView.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setAllowFileAccess(true); s.setAllowContentAccess(true); s.setMediaPlaybackRequiresUserGesture(false); webView.setWebViewClient(new WebViewClient()); webView.setWebChromeClient(new WebChromeClient()); webView.loadUrl("'+start+'"); } @Override public void onBackPressed() { if(webView!=null && webView.canGoBack()) webView.goBack(); else super.onBackPressed(); } }\n')
    (javadir/'MainActivity.java').write_text(java,encoding='utf-8')
    label=name.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    manifest=('''<manifest xmlns:android="http://schemas.android.com/apk/res/android">
<uses-permission android:name="android.permission.INTERNET" />
<application android:label="'''+label+'''" android:theme="@android:style/Theme.Material.Light.NoActionBar" android:usesCleartextTraffic="true">
<activity android:name=".MainActivity" android:exported="true"><intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity>
</application></manifest>''')
    (main/'AndroidManifest.xml').write_text(manifest,encoding='utf-8')
def worker(build_id,data):
    work=WORK_DIR/build_id; uploaded=work/'uploaded'; project=work/'project'
    try:
        update_job(build_id,status='building',progress=5,message='Preparing project files.')
        count,total=(0,0)
        if data.get('source_type','html')=='html': count,total=receive_files(data,uploaded)
        make_project(project,uploaded,data); update_job(build_id,progress=15,message='Running Gradle APK build.')
        gradle=shutil.which('gradle')
        if not gradle: raise RuntimeError('Gradle was not found in the container.')
        env=os.environ.copy(); env['ANDROID_SDK_ROOT']=SDK; env['ANDROID_HOME']=SDK
        proc=subprocess.run([gradle,'--no-daemon','--stacktrace','assembleDebug'],cwd=str(project),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,universal_newlines=True,timeout=BUILD_TIMEOUT)
        if proc.returncode!=0:
            app.logger.error('Gradle failed for %s:\n%s',build_id,(proc.stdout or '')[-12000:]); raise RuntimeError('Gradle build failed. Check Render logs for details.')
        apk=project/'app'/'build'/'outputs'/'apk'/'debug'/'app-debug.apk'
        if not apk.is_file() or apk.stat().st_size==0: raise RuntimeError('APK output was not found after Gradle completed.')
        final=APK_DIR/(build_id+'.apk'); shutil.copy2(apk,final)
        base=os.environ.get('PUBLIC_BASE_URL','https://sketchware-html-create.onrender.com').rstrip('/')
        update_job(build_id,status='completed',progress=100,message='APK built successfully.',download_url=base+'/api/build/'+build_id+'/download',apk_size_bytes=final.stat().st_size,file_count=count,total_file_bytes=total)
    except subprocess.TimeoutExpired: update_job(build_id,status='failed',progress=100,message='Gradle build timed out after 600 seconds.')
    except ValueError as e: update_job(build_id,status='failed',progress=100,message=str(e)[:500])
    except Exception as e:
        app.logger.exception('Build failed for %s',build_id); update_job(build_id,status='failed',progress=100,message=str(e)[:500])
    finally:
        try:
            if work.exists(): shutil.rmtree(work)
        except Exception: app.logger.warning('Could not clean temporary build folder.')

@app.route('/')
def home():
    return jsonify({'service':SERVICE_NAME,'status':'online','version':SERVICE_VERSION,'endpoints':{'health':'/health','engine_check':'/api/build/engine-check','create_build':'POST /api/build','build_status':'GET /api/build/<build_id>','download_apk':'GET /api/build/<build_id>/download'},'build_engine':'gradle_debug_apk'})
@app.route('/health')
def health(): return jsonify({'status':'ok','service':SERVICE_NAME,'time':now()})
@app.route('/api/build/engine-check')
def engine_check():
    sdk=Path(SDK); java=shutil.which('java'); gradle=shutil.which('gradle'); sdkm=shutil.which('sdkmanager')
    result={'success':True,'java':bool(java),'java_path':java,'gradle':bool(gradle),'gradle_path':gradle,'sdkmanager':bool(sdkm),'sdkmanager_path':sdkm,'android_sdk_root':SDK,'sdk_directory_exists':sdk.is_dir(),'platform_android_35_exists':(sdk/'platforms/android-35').is_dir(),'build_tools_35_exists':(sdk/'build-tools/35.0.0').is_dir(),'time':now()}
    result['ready_for_apk_build']=bool(java and gradle and sdkm and result['sdk_directory_exists'] and result['platform_android_35_exists'] and result['build_tools_35_exists'])
    result['note']='Tools detected. A real build request is needed to validate compilation.'
    return jsonify(result)
@app.route('/api/build',methods=['POST'])
def create_build():
    data=request.get_json(silent=True)
    if not isinstance(data,dict): return err('A JSON request body is required.')
    build_id=str(uuid.uuid4()); stamp=now()
    job={'build_id':build_id,'project_id':str(data.get('project_id','')),'status':'queued','message':'Build request accepted.','created_at':stamp,'updated_at':stamp,'progress':0,'download_url':None}
    with LOCK: JOBS[build_id]=job
    save_job(dict(job)); threading.Thread(target=worker,args=(build_id,data),daemon=True).start()
    return jsonify(dict(job)),202
@app.route('/api/build/<build_id>',methods=['GET'])
def status(build_id):
    try: uuid.UUID(build_id)
    except Exception: return err('Invalid build ID.')
    job=get_job(build_id)
    if job is None: return err('Build job not found.',404)
    return jsonify(job)
@app.route('/api/build/<build_id>/download',methods=['GET'])
def download(build_id):
    try: uuid.UUID(build_id)
    except Exception: return err('Invalid build ID.')
    job=get_job(build_id)
    if job is None: return err('Build job not found.',404)
    if job.get('status')!='completed': return err('APK is not ready yet.',409)
    apk=APK_DIR/(build_id+'.apk')
    if not apk.is_file(): return err('APK is unavailable on this server instance. Configure persistent storage.',404)
    return send_file(str(apk),as_attachment=True,download_name='html-app-'+build_id[:8]+'.apk',mimetype='application/vnd.android.package-archive')
@app.errorhandler(413)
def too_large(_): return err('Request exceeds the 50 MB limit.',413)
@app.errorhandler(500)
def internal_error(_):
    app.logger.exception('Internal server error'); return err('Internal server error.',500)
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','10000')),debug=False)
