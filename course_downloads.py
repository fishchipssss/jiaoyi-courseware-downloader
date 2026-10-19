"""Stream course files through the authenticated school's current URL mapping."""
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests


def file_is_valid(path):
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open('rb') as handle:
        signature = handle.read(8)
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        return signature.startswith(b'%PDF-')
    if suffix == '.ppt':
        return signature == bytes.fromhex('d0cf11e0a1b11ae1')
    if suffix == '.pptx':
        try:
            with zipfile.ZipFile(path) as archive:
                return 'ppt/presentation.xml' in archive.namelist()
        except (OSError, zipfile.BadZipFile):
            return False
    return False


def resolve_download_url(page, original):
    if urlparse(page.url).hostname != 'webvpn2.shsmu.edu.cn':
        return original
    rewritten = page.evaluate(
        '(url) => typeof vpn_rewrite_url === "function" ? vpn_rewrite_url(url) : null',
        original,
    )
    if not isinstance(rewritten, str) or urlparse(rewritten).hostname != 'webvpn2.shsmu.edu.cn':
        raise RuntimeError('学校 WebVPN 没有提供课件地址转换，请重新打开课表。')
    return rewritten


def allowed_file_auth(original, resolved):
    source, target = urlparse(original), urlparse(resolved)
    if source.hostname != 'curriculum.shsmu.edu.cn' or not source.path.startswith('/docs/'):
        return False
    return target.scheme == 'https' and (
        target.hostname == source.hostname and target.path.startswith('/docs/')
        or target.hostname == 'webvpn2.shsmu.edu.cn' and '/docs/' in target.path
    )


def stream_file(session, url, destination, authorization=None, timeout=600):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + '.part')
    start = time.monotonic()
    response = None
    try:
        # Never send course credentials to a login redirect or an unrelated page.
        response = session.get(url, stream=True, allow_redirects=False, timeout=(15, 30))
        if response.status_code == 401 and authorization:
            response.close()
            response = session.get(
                url, headers={'Authorization': authorization}, stream=True,
                allow_redirects=False, timeout=(15, 30),
            )
        if response.status_code != 200:
            if response.is_redirect:
                return False, '登录会话已失效，文件请求被重定向；请重新登录。'
            return False, f'HTTP {response.status_code}'
        content_type = response.headers.get('content-type', '').lower()
        if 'text/html' in content_type or 'application/json' in content_type:
            return False, '服务器返回登录页或错误页面，没有返回课件。'
        expected = int(response.headers.get('content-length') or 0)
        received = 0
        last_progress = start
        total_label = f'{expected / 1048576:.1f} MB' if expected else '服务器未提供总大小'
        print(f"开始传输：{destination.name} | {total_label}", flush=True)
        with partial.open('wb') as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if time.monotonic() - start > timeout:
                    raise TimeoutError(f'超过单文件下载时限 {timeout} 秒')
                if chunk:
                    handle.write(chunk)
                    received += len(chunk)
                if time.monotonic() - last_progress >= 5:
                    print(f"下载进度：{destination.name} | 已接收 {received / 1048576:.1f} MB", flush=True)
                    last_progress = time.monotonic()
        if expected and not response.headers.get('content-encoding') and expected != received:
            raise IOError(f'文件传输不完整：{received}/{expected} 字节')
        # Validate using the intended extension before publishing the completed file.
        if destination.suffix.lower() == '.pptx':
            print(f'校验课件：{destination.name}', flush=True)
            with zipfile.ZipFile(partial) as archive:
                if 'ppt/presentation.xml' not in archive.namelist():
                    raise ValueError('文件不是有效的 PPTX 课件')
                if archive.testzip() is not None:
                    raise ValueError('PPTX 文件内容校验失败，传输不完整或源文件已损坏')
        else:
            with partial.open('rb') as handle:
                signature = handle.read(8)
            if destination.suffix.lower() == '.pdf' and not signature.startswith(b'%PDF-'):
                raise ValueError('文件不是有效的 PDF')
            if destination.suffix.lower() == '.ppt' and signature != bytes.fromhex('d0cf11e0a1b11ae1'):
                raise ValueError('文件不是有效的 PPT')
        partial.replace(destination)
        return True, 'ok'
    except zipfile.BadZipFile:
        return False, '下载内容缺少完整的 PPTX 文件结构，可能是源文件或传输内容被截断。'
    except (requests.RequestException, OSError, ValueError) as exc:
        # Avoid dumping requests' headers, which can contain Basic credentials.
        return False, f'{type(exc).__name__}: {str(exc).split(chr(10))[0]}'
    finally:
        if response is not None:
            response.close()
        partial.unlink(missing_ok=True)


def download_week(page, context, records, output_root, args):
    from download_ppt_by_week import basic_auth_header, record_label, safe_filename, unique_path

    session = requests.Session()
    for cookie in context.cookies():
        session.cookies.set(
            cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'],
            secure=cookie.get('secure', False),
        )
    session.headers.update({'User-Agent': page.evaluate('navigator.userAgent')})
    output_root = Path(output_root).resolve()
    pending = list(records)
    results = {}
    used_paths = {}
    try:
        for attempt in range(2):
            retry = []
            for record in pending:
                source = record['url']
                key = source
                directory = output_root / record['week']
                directory.mkdir(parents=True, exist_ok=True)
                manifest = directory / 'download-results.json'
                try:
                    previous = json.loads(manifest.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    previous = {}
                known = previous.get(key, {})
                known_name = known.get('filename', '')
                if known_name and Path(known_name).name == known_name and file_is_valid(directory / known_name):
                    results[key] = known
                    print(f"已下载，跳过：{record_label(record)}", flush=True)
                    continue
                name = safe_filename(record['fallback_name'], 'courseware.pptx')
                destination = used_paths.setdefault(key, unique_path(directory / name))
                print(f"{'重试' if attempt else '下载课件'}：{record_label(record)}", flush=True)
                try:
                    url = resolve_download_url(page, source)
                    auth = basic_auth_header(args.file_username, args.file_password) if allowed_file_auth(source, url) and args.file_username and args.file_password else None
                    ok, reason = stream_file(session, url, destination, auth)
                except Exception as exc:
                    ok, reason = False, str(exc).split('\n')[0]
                result = {**record, 'filename': destination.name if ok else '', 'success': ok, 'reason': reason}
                results[key] = result
                previous[key] = result
                manifest.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding='utf-8')
                if ok:
                    print(f'Downloaded: {destination}', flush=True)
                else:
                    print(f'Download failed, continuing: {record_label(record)} | {reason}', flush=True)
                    if reason not in ('HTTP 401', 'HTTP 403', 'HTTP 404'):
                        retry.append(record)
            pending = retry
            if not pending:
                break
    finally:
        session.close()
    failures = [record for record in results.values() if not record.get('success')]
    for week in {record['week'] for record in results.values()}:
        week_results = [record for record in results.values() if record['week'] == week]
        report_lines = ['课件下载结果', '']
        for record in week_results:
            status = '成功' if record.get('success') else '失败'
            report_lines.append(f"{status} | {record_label(record)}")
            report_lines.append(f"  {record.get('filename') or record.get('reason', '')}")
            if not record.get('success'):
                report_lines.append(f"  链接：{record['url']}")
        (output_root / week / '下载结果.txt').write_text('\n'.join(report_lines) + '\n', encoding='utf-8-sig')
    if failures:
        print('Failed downloads:', flush=True)
        for record in failures:
            print(f"- {record_label(record)} | {record['reason']}", flush=True)
    print(f'Download summary: success={len(results) - len(failures)} failed={len(failures)}', flush=True)
    return len(failures)
