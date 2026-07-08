import os
import re
import base64
import zipfile
import shutil
import tempfile
from email.parser import BytesParser
from email import policy
from email.message import EmailMessage
from typing import List, Optional

# -------------------- 可调参数 --------------------
FILE_EXIST_AUTO_REWRITE = 1          # 1: 自动覆盖, 0: 询问
IMG_NAME_REWRITE = 1                 # 1: 重命名为 0001.jpg, 0: 使用原始文件名
MIN_IMG_SIZE_BYTES = 50 * 1024       # 小于该大小的图片将被跳过（50KB）
# ------------------------------------------------

def sanitize_filename(filename: str) -> str:
    """移除路径遍历字符和非法字符，仅保留安全字符"""
    # 只保留字母数字、下划线、点、横杠
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(filename))

def get_img_extension(content_type: str) -> str:
    """从Content-Type中提取标准扩展名，若无则默认 .bin"""
    # 示例: 'image/jpeg' -> 'jpg', 'image/png' -> 'png'
    if ';' in content_type:
        content_type = content_type.split(';')[0].strip()
    mapping = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/gif': 'gif',
        'image/bmp': 'bmp',
        'image/webp': 'webp',
        'image/svg+xml': 'svg',
    }
    return mapping.get(content_type.lower(), 'bin')

def decode_base64_safely(data: bytes) -> Optional[bytes]:
    """安全解码base64，自动去除所有空白字符"""
    try:
        # 去除所有空白字符（空格、换行、回车等）
        clean = b''.join(data.split())
        return base64.b64decode(clean, validate=False)
    except Exception:
        return None

def extract_images_from_mht(mht_path: str, output_dir: str) -> List[str]:
    """从mht文件中提取所有图片，返回保存的图片路径列表"""
    saved_images = []
    try:
        with open(mht_path, 'rb') as fp:
            # 使用BytesParser解析MIME消息
            msg: EmailMessage = BytesParser(policy=policy.default).parse(fp)

        # 遍历所有附件和内联资源
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = part.get('Content-Disposition', '')
            content_location = part.get('Content-Location', '')
            transfer_encoding = part.get('Content-Transfer-Encoding', '').lower()

            # 只处理 image 类型且编码为 base64 的部分
            if not content_type.startswith('image/') or transfer_encoding != 'base64':
                continue

            # 获取原始数据（已经过解码，但未解码base64内容）
            payload = part.get_payload(decode=False)  # 返回字符串（根据编码转换，我们使用原始字节）
            if not isinstance(payload, str):
                # 对于bytes，直接使用
                raw_data = payload
            else:
                # 如果是str，转为bytes（通常base64不会自动解码）
                raw_data = payload.encode('utf-8')

            # 安全解码base64
            img_bytes = decode_base64_safely(raw_data)
            if img_bytes is None:
                print(f"  [WARN] Base64解码失败: {content_location}")
                continue

            # 过滤小图片（可选）
            if len(img_bytes) < MIN_IMG_SIZE_BYTES:
                print(f"  [SKIP] 图片太小 ({len(img_bytes)} bytes): {content_location}")
                continue

            # 确定文件名
            if IMG_NAME_REWRITE == 1:
                # 使用从1开始的连续编号，4位补零
                ext = get_img_extension(content_type)
                img_name = f"{len(saved_images)+1:04d}.{ext}"
            else:
                # 从Content-Location中提取文件名
                if content_location:
                    base = os.path.basename(content_location.split('?')[0])
                else:
                    # 如果没有Content-Location，使用随机名
                    base = f"img_{len(saved_images)+1}"
                # 分离扩展名（如果没有扩展名，使用内容类型）
                name, ext = os.path.splitext(base)
                if not ext:
                    ext = '.' + get_img_extension(content_type)
                img_name = sanitize_filename(name + ext)

            # 防止重名：如果已存在，添加序号
            target_path = os.path.join(output_dir, img_name)
            counter = 1
            while os.path.exists(target_path):
                base, ext = os.path.splitext(img_name)
                new_name = f"{base}_{counter}{ext}"
                target_path = os.path.join(output_dir, new_name)
                counter += 1

            # 保存图片
            try:
                with open(target_path, 'wb') as f:
                    f.write(img_bytes)
                saved_images.append(target_path)
                print(f"  [SAVED] {os.path.basename(target_path)} ({len(img_bytes)} bytes)")
            except IOError as e:
                print(f"  [ERROR] 保存失败: {target_path} - {e}")

    except Exception as e:
        print(f"  [ERROR] 处理MHT文件时出错: {e}")
    return saved_images

def get_dir_size(dir_path: str) -> int:
    total = 0
    for root, _, files in os.walk(dir_path):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

def zip_images(source_dir: str, zip_path: str):
    """将目录中的图片打包为zip，尝试使用压缩，失败则使用存储"""
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(source_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    arcname = os.path.relpath(full_path, source_dir)
                    zf.write(full_path, arcname)
    except RuntimeError:  # zlib not available
        print("  [WARN] 压缩不可用，使用存储模式")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for root, _, files in os.walk(source_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    arcname = os.path.relpath(full_path, source_dir)
                    zf.write(full_path, arcname)

def process_mht_file(mht_file: str):
    """处理单个.mht文件，创建同名目录，提取图片，打包为zip"""
    base_name = os.path.splitext(mht_file)[0]
    cur_dir = os.getcwd()
    output_dir = os.path.join(cur_dir, base_name)

    # 检查输出目录是否存在
    if os.path.exists(output_dir):
        if FILE_EXIST_AUTO_REWRITE == 1:
            print(f"  [INFO] 目录已存在，自动覆盖 (清空)")
            shutil.rmtree(output_dir)  # 删除旧目录
        else:
            print(f"  [WARN] 目录 '{base_name}' 已存在，是否覆盖? (y/n)")
            confirm = input().strip().lower()
            if confirm != 'y':
                print("  [SKIP] 跳过该文件")
                return
            shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)
    print(f"  [创建目录] {output_dir}")

    # 提取图片
    saved = extract_images_from_mht(mht_file, output_dir)
    if not saved:
        print("  [WARN] 未提取到任何图片，删除空目录")
        os.rmdir(output_dir)  # 仅当为空，但可能已有文件？我们已删除并新建，若没保存，则移除。
        return

    # 打包为zip
    zip_path = os.path.join(cur_dir, base_name + '.zip')
    try:
        zip_images(output_dir, zip_path)
        raw_size = get_dir_size(output_dir)
        zip_size = os.path.getsize(zip_path)
        print(f"  [压缩完成] 原始大小: {raw_size/1024/1024:.2f} MB, ZIP: {zip_size/1024/1024:.2f} MB")
    except Exception as e:
        print(f"  [ERROR] 打包失败: {e}")

    # 可选：删除目录以节省空间（如需要可注释掉）
    # shutil.rmtree(output_dir)

def get_mht_files() -> List[str]:
    """返回当前目录下所有 .mht 文件（不区分大小写）"""
    files = [f for f in os.listdir('.') if os.path.isfile(f) and f.lower().endswith('.mht')]
    return files

def main():
    target_files = get_mht_files()
    if not target_files:
        print("当前目录未找到任何 .mht 文件")
        return
    print(f"找到 {len(target_files)} 个 MHT 文件：")
    for f in target_files:
        print(f"  - {f}")

    for idx, f in enumerate(target_files, 1):
        print(f"\n===== 处理 [{idx}/{len(target_files)}]: {f} =====")
        try:
            process_mht_file(f)
        except Exception as e:
            print(f"  [CRITICAL] 处理 '{f}' 时发生严重错误，跳过: {e}")

if __name__ == "__main__":
    main()