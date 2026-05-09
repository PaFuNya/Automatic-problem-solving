import json
import base64
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# 全局配置变量 (CSS 选择器与 API 密钥)
# ==========================================

# Chrome CDP 调试端口地址
CDP_URL = "http://localhost:9222"

# 大模型 API 配置 (改回 DeepSeek)
LLM_API_KEY = "sk*****"
LLM_API_URL = "https://api.deepseek.com/chat/completions" # DeepSeek API
LLM_MODEL_NAME = "deepseek-chat" # DeepSeek 对话模型

# 课程与视频相关选择器
# 已在主循环中使用动态 XPath 替代固定选择器，以支持更精确的父级点击
# SELECTOR_UNFINISHED_CHAPTER = "text='未学习'" 
SELECTOR_VIDEO = "video"                                      # 视频播放器元素
SELECTOR_BACK_TO_OUTLINE = ".back-to-outline-btn"             # 返回目录按钮

# 测验相关选择器
SELECTOR_START_QUIZ = "text='开始测验'"                       # 开始测验按钮
SELECTOR_QUESTION_AREA = ".van-list"                          # 题目整体区域 (根据截图调整)
SELECTOR_OPTION_LABEL = ".van-radio, .van-checkbox, label"    # 选项标签 (根据截图调整)
SELECTOR_SUBMIT_BTN = "text='提交问卷'"                       # 提交答案按钮 (根据截图)
SELECTOR_BACK_BTN = ".van-icon-arrow-left"                    # 返回按钮 (根据截图)

# ==========================================
# 核心功能模块
# ==========================================

def get_ai_answer(question_text: str) -> dict:
    """
    调用大模型 API，传入题目文本，返回 JSON 格式的答案。
    """
    system_prompt = (
        "你是一个做题助手。请阅读以下包含多道选择题的文本。"
        "你必须为每道题给出一个答案。如果选项以字母开头(如 'A. 内容' 或 'A 内容')，你必须连同字母和标点一起返回完整的一行，例如返回 'A. 觉得自己被同学用病毒攻击' 而不能只返回 'A' 或缺少标点。"
        "重要提示：你返回的 target_text 必须与网页上的选项文本在字面上（包含标点符号和空格）尽可能一致！"
        "必须严格输出为 JSON 数组格式，每一项代表一道题的答案，"
        "例如：\n"
        "[\n"
        "  {\"answer_type\": \"single\", \"target_text\": \"A. 觉得自己被同学用病毒攻击\"},\n"
        "  {\"answer_type\": \"multiple\", \"target_text\": [\"A. 是一组病因未明的重性精神病\", \"B. 多起病于青壮年，患者一般无意识障碍\"]}\n"
        "]\n"
        "不要包含任何其他废话或外层对象，直接返回 JSON 数组。"
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }

    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": f"请给出这道题的答案，题目内容如下：\n\n{question_text}"
            }
        ],
        "response_format": {"type": "json_object"} # 强制要求 JSON 输出
    }

    try:
        print("[AI] 正在请求大模型...")
        response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=60)
        
        # 增加错误信息打印，方便排查
        if response.status_code != 200:
            print(f"[错误] API 返回状态码: {response.status_code}")
            print(f"[错误] API 错误详情: {response.text}")
            response.raise_for_status()
            
        result_text = response.json()['choices'][0]['message']['content']
        print(f"[AI] 模型原始返回: {result_text}")
        
        # 解析 JSON 结果
        answer_data = json.loads(result_text)
        return answer_data
    except Exception as e:
        print(f"[错误] API 调用或解析失败: {e}")
        return {}

def skip_video(page):
    """
    处理视频播放页面，强制跳转到结尾
    """
    print("[流程] 尝试查找并跳过视频...")
    try:
        # 等待 video 元素加载
        page.wait_for_selector(SELECTOR_VIDEO, state="attached", timeout=10000)
        
        # 注入 JS 强制跳过进度
        page.evaluate('''() => {
            var video = document.querySelector('video');
            if(video){
                // 确保视频静音，有些浏览器限制非静音视频自动播放
                video.muted = true; 
                // 设置进度到结束前 1 秒
                video.currentTime = video.duration > 1 ? video.duration - 1 : 0; 
                video.play().catch(e => console.log("自动播放被阻止", e));
            }
        }''')
        print("[流程] 视频已快进至结尾，等待系统记录进度 (5秒)...")
        
        # 停留几秒确保平台接收到进度日志
        time.sleep(5)
        
        # 因为跳时间之后页面会自动出现点击测验按钮，所以不需要 reload，直接结束本轮 skip_video 即可
            
    except PlaywrightTimeoutError:
        print("[流程] 当前页面未发现视频，跳过处理。")
    except Exception as e:
        print(f"[错误] 视频跳过失败: {e}")

def solve_quiz(page):
    """
    处理测验页面：截图、调用AI、回填答案并提交
    """
    print("[流程] 检测到测验，开始答题...")
    try:
        # 等待页面加载，这通常是个移动端页面适配，等待列表或题目区域加载
        page.wait_for_load_state("networkidle", timeout=10000)
        time.sleep(2)
        
        # 很多移动端UI组件库（如Vant）包裹题目，我们可以直接对整个 body 截图，如果太长再考虑截特定区域
        # 尝试寻找 .van-list (Vant的列表组件)，找不到就用 body
        if page.locator(".van-list").is_visible():
             question_element = page.locator(".van-list").first
        else:
             question_element = page.locator("body")
             
        print("[调试] 正在提取题目文本...")
        # 直接从网页 DOM 提取纯文本，速度比 OCR 快 100 倍且 100% 准确
        question_text = question_element.inner_text()
        print(f"[调试] 提取到的题目内容预览: {question_text[:100]}...")
        
        # 将页面上所有的选项文本提取出来，和 AI 返回的答案做校验，这有助于避免格式错误
        # 获取网页上所有实际渲染出来的选项
        raw_options = page.locator("xpath=//div[contains(@class, 'van-radio') or contains(@class, 'van-checkbox') or contains(@class, 'el-radio')]").all_inner_texts()
        raw_options = [opt.strip() for opt in raw_options if opt.strip()]
        
        # 调用大模型获取答案
        answer_data = get_ai_answer(question_text)
        if not answer_data:
            print("[错误] 未获取到有效答案，跳过本题。")
            return
            
        # 如果大模型外层包裹了字典(比如 {"answers": [...]})，提取出来
        if isinstance(answer_data, dict):
             # 尝试寻找常见的列表键
             for key in ["answers", "data", "results"]:
                  if key in answer_data and isinstance(answer_data[key], list):
                       answer_data = answer_data[key]
                       break
             else:
                  # 如果没找到，尝试把字典当成单题包装成列表
                  if "answer_type" in answer_data:
                       answer_data = [answer_data]
                  else:
                       print(f"[错误] 无法解析大模型返回的字典结构: {answer_data}")
                       return

        if not isinstance(answer_data, list):
            print(f"[错误] 解析出的答案不是列表结构: {answer_data}")
            return
            
        # 遍历每一道题的答案进行回填
        for idx, answer in enumerate(answer_data):
            answer_type = answer.get("answer_type")
            target_text = answer.get("target_text")
            
            print(f"[流程] AI 判断第 {idx+1} 题为 {answer_type} 题，准备点击选项: {target_text}")
            
            if answer_type == "single":
                core_text = target_text
                if len(target_text) > 2 and target_text[1] in [".", "、", " ", "．"]:
                    core_text = target_text[2:].strip()
                elif len(target_text) > 1 and target_text[0] in "ABCD":
                    core_text = target_text[1:].strip()
                
                # 最强暴力点击：找到页面上所有包含该文本的选项
                all_matching_options = page.locator(f"xpath=//div[contains(@class, 'van-radio') or contains(@class, 'van-checkbox') or contains(@class, 'el-radio')]//span[contains(text(), '{core_text}')]")
                
                if all_matching_options.count() == 0:
                    all_matching_options = page.locator(f"text=/{core_text}/")
                
                match_count = all_matching_options.count()
                if match_count > 0:
                    if match_count > 1:
                        print(f"[调试] 发现 {match_count} 个名为 '{core_text}' 的选项。正在尝试根据题目顺序精准点击...")
                        
                        # 改回严格基于 DOM 结构的相对定位法，这是唯一能100%防止跨题点错的方法
                        # 不再使用全局匹配后猜第几个，而是强制在“当前这道题”的 DOM 区块里找！
                        question_blocks = page.locator(".question-item, .van-cell-group, .van-list > div, .question-wrapper")
                        if question_blocks.count() > idx:
                            # 缩小范围：只在第 idx 题的区块里找
                            current_block = question_blocks.nth(idx)
                            block_options = current_block.locator(f"xpath=.//div[contains(@class, 'van-radio') or contains(@class, 'van-checkbox') or contains(@class, 'el-radio')]//span[contains(text(), '{core_text}')]")
                            
                            if block_options.count() > 0:
                                block_options.first.click(force=True)
                                print(f"[调试] (区块定位) 成功点击了选项: {target_text}")
                            else:
                                # 如果区块里找不到，说明区块划分可能有问题，退回全局寻找未选中的
                                print(f"[警告] 区块内未找到 '{core_text}'，尝试全局回退...")
                                unselected = page.locator(f"xpath=//div[(contains(@class, 'van-radio') or contains(@class, 'van-checkbox')) and not(contains(@class, 'checked'))]//span[contains(text(), '{core_text}')]").first
                                if unselected.is_visible():
                                    unselected.click(force=True)
                                    print(f"[调试] (回退) 点击了未选中的选项: {target_text}")
                                else:
                                    all_matching_options.first.click(force=True)
                        else:
                            # 如果根本找不到题目区块，使用未选中策略
                            unselected = page.locator(f"xpath=//div[(contains(@class, 'van-radio') or contains(@class, 'van-checkbox')) and not(contains(@class, 'checked'))]//span[contains(text(), '{core_text}')]").first
                            if unselected.is_visible():
                                unselected.click(force=True)
                                print(f"[调试] (无区块回退) 点击了未选中的选项: {target_text}")
                            else:
                                all_matching_options.last.click(force=True) if idx > 0 else all_matching_options.first.click(force=True)
                            
                    else:
                        all_matching_options.first.click(force=True)
                        print(f"[调试] 成功点击了选项: {target_text}")
                else:
                    print(f"[警告] 未在页面上找到包含核心文本 '{core_text}' 或 '{target_text}' 的选项！")
                    
            elif answer_type == "multiple" and isinstance(target_text, list):
                for t in target_text:
                    core_text = t
                    if len(t) > 2 and t[1] in [".", "、", " ", "．"]:
                        core_text = t[2:].strip()
                    elif len(t) > 1 and t[0] in "ABCD":
                        core_text = t[1:].strip()
                        
                    all_matching_options = page.locator(f"xpath=//div[contains(@class, 'van-radio') or contains(@class, 'van-checkbox') or contains(@class, 'el-radio')]//span[contains(text(), '{core_text}')]")
                    if all_matching_options.count() == 0:
                        all_matching_options = page.locator(f"text=/{core_text}/")
                        
                    match_count = all_matching_options.count()
                    if match_count > 0:
                        if match_count > 1:
                            question_blocks = page.locator(".question-item, .van-cell-group, .van-list > div, .question-wrapper")
                            if question_blocks.count() > idx:
                                current_block = question_blocks.nth(idx)
                                block_options = current_block.locator(f"xpath=.//div[contains(@class, 'van-radio') or contains(@class, 'van-checkbox') or contains(@class, 'el-radio')]//span[contains(text(), '{core_text}')]")
                                
                                if block_options.count() > 0:
                                    block_options.first.click(force=True)
                                    print(f"[调试] (区块多选定位) 成功点击了多选选项: {t}")
                                else:
                                    unselected = page.locator(f"xpath=//div[(contains(@class, 'van-radio') or contains(@class, 'van-checkbox')) and not(contains(@class, 'checked'))]//span[contains(text(), '{core_text}')]").first
                                    if unselected.is_visible():
                                        unselected.click(force=True)
                                        print(f"[调试] (多选回退) 点击了未选中的选项: {t}")
                                    else:
                                        all_matching_options.first.click(force=True)
                            else:
                                unselected = page.locator(f"xpath=//div[(contains(@class, 'van-radio') or contains(@class, 'van-checkbox')) and not(contains(@class, 'checked'))]//span[contains(text(), '{core_text}')]").first
                                if unselected.is_visible():
                                    unselected.click(force=True)
                                else:
                                    all_matching_options.last.click(force=True) if idx > 0 else all_matching_options.first.click(force=True)
                        else:
                            all_matching_options.first.click(force=True)
                            print(f"[调试] 成功点击了多选选项: {t}")
                    else:
                        print(f"[警告] 未在页面上找到包含核心文本 '{core_text}' 或 '{t}' 的多选选项！")
                    time.sleep(0.5) # 稍微延迟避免点击过快
                    
            time.sleep(1) # 每题答完等待动画和状态生效
        
        # 提交答案
        submit_btn = page.locator(SELECTOR_SUBMIT_BTN)
        if submit_btn.is_visible() and submit_btn.is_enabled():
            submit_btn.click(force=True)
            print("[流程] 点击了提交按钮。")
            time.sleep(2)
            
            # 处理 Vant 组件库的确认弹窗
            # 弹窗的 DOM 结构通常是: <div role="dialog" class="van-dialog">... <button>确认</button>
            # 根据提供的完整结构，确认按钮带有 van-dialog__confirm 类
            confirm_btn = page.locator(".van-dialog__confirm").first
            if confirm_btn.is_visible():
                confirm_btn.click(force=True)
                print("[流程] 已确认提交。")
                time.sleep(2)
        else:
            print("[错误] 提交按钮不可见或被禁用，可能因为没选上任何选项！")
            return # 如果没法提交，就提前退出，别去点返回了
        time.sleep(3)
        
        # 尝试点击返回目录 (如果需要)
        # 根据日志，测验完成后左上角有一个返回按钮
        if page.locator(SELECTOR_BACK_BTN).is_visible():
            page.locator(SELECTOR_BACK_BTN).click()
            print("[流程] 点击了返回按钮。")
            time.sleep(2)
        elif page.locator("text='返回'").is_visible():
            page.locator("text='返回'").click()
            time.sleep(2)
        
    except Exception as e:
        print(f"[错误] 答题流程出现异常: {e}")

def run_auto_course():
    """
    主控制流：接管浏览器、循环刷课与答题
    """
    with sync_playwright() as p:
        print(f"[系统] 正在连接本地 Chrome (CDP: {CDP_URL})...")
        try:
            # 接管本地已登录的 Chrome
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            
            # 找到目标网站所在的标签页
            page = None
            for p in context.pages:
                # 排除开发者工具页面和 Chrome 内部系统页面
                if not p.url.startswith("devtools://") and not p.url.startswith("chrome://"):
                    page = p
                    break
            
            if page is None:
                print("[严重错误] 未找到常规网页，请确保你在 Chrome 中打开了课程页面！")
                print("当前浏览器所有打开的标签页如下：")
                for p in context.pages:
                    print(f" - {p.url}")
                return
                
            # 激活该页面，将其带到前台
            page.bring_to_front()
            print(f"[系统] 已锁定目标页面 URL: {page.url}")
            
        except Exception as e:
            print(f"[严重错误] 无法连接到浏览器，请确保 Chrome 已开启 debugging port。错误信息: {e}")
            return
            
        # 注册自动处理 Dialog (如 alert/confirm)
        page.on("dialog", lambda dialog: dialog.accept())
        
        print("[系统] 接管成功，开始执行自动化任务。")
        
        # 持续循环，直到所有课程完成
        while True:
            try:
                # 检查是否存在测验按钮，优先处理测验
                quiz_buttons = page.locator(SELECTOR_START_QUIZ)
                if quiz_buttons.count() > 0:
                    print(f"[流程] 发现 {quiz_buttons.count()} 个测验按钮，进入测验...")
                    quiz_buttons.first.click(force=True)
                    
                    # 避免频繁点击导致页面卡死，稍微等一下让页面反应
                    time.sleep(3)
                    # 给页面跳转预留加载时间
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    
                    solve_quiz(page)
                    continue

                # 如果当前页面已经在答题页面（有时候测验按钮点完不会跳转，而是直接展示题目）
                # 我们通过判断页面上有没有 "提交问卷" 按钮来判断是否在答题状态
                if page.locator(SELECTOR_SUBMIT_BTN).is_visible():
                    print("[流程] 检测到当前处于答题页面，直接开始答题...")
                    solve_quiz(page)
                    continue

                # 查找未完成的章节
                # 使用 Playwright 推荐的内置文本选择器直接点击文本所在的最近元素
                unfinished_chapters = page.locator("text='未学习'")
                
                # 同时我们也查找 "正在学" 的章节，因为视频可能处于正在播放状态
                learning_chapters = page.locator("text='正在学'")
                
                if learning_chapters.count() > 0:
                    print(f"[流程] 发现 {learning_chapters.count()} 个'正在学'章节，准备处理当前视频...")
                    # 尝试跳过视频
                    skip_video(page)
                elif unfinished_chapters.count() > 0:
                    print(f"[流程] 发现 {unfinished_chapters.count()} 个未完成章节，尝试通过点击标题进入下一个...")
                    
                    # 由于直接点击"未学习"及其父元素无效，我们改变策略：
                    # 找到"未学习"元素，然后在其所在的整个章节条目（li/div）中，寻找真正的标题元素（通常是 a, span 或带有特定 class 的 div）进行点击
                    first_unfinished = unfinished_chapters.first
                    if first_unfinished.is_visible():
                        # 找到包裹当前章节的整体卡片（我们往上找最近的带有 class 的区块，或者直接找最近的 li）
                        chapter_container = first_unfinished.locator("xpath=./ancestor::*[contains(@class, 'course-item') or contains(@class, 'chapter') or local-name()='li'][1]")
                        
                        try:
                            if chapter_container.count() > 0:
                                # 找到卡片后，点击它里面最有可能绑定事件的区域（通常是排在前面的文本/标题元素）
                                # 我们先尝试点击里面的第一个带文本的元素
                                title_element = chapter_container.locator("xpath=.//*[text() and not(contains(text(), '未学习'))]").first
                                if title_element.count() > 0:
                                    title_element.click(force=True)
                                    print(f"[调试] 成功点击了该章节的标题部分: {title_element.inner_text().strip()[:20]}")
                                else:
                                    # 退而求其次，点击整个卡片
                                    chapter_container.click(force=True)
                                    print("[调试] 点击了包含'未学习'的整个章节卡片区域")
                            else:
                                # 如果找不到特征明显的父容器，执行最后的回退方案
                                first_unfinished.locator("xpath=./..").click(force=True)
                                print("[调试] 执行了保底的父节点点击")
                        except Exception as e:
                            print(f"[错误] 点击章节卡片失败: {e}")
                    
                    # 避免频繁点击导致页面卡死，稍微等一下让页面反应
                    time.sleep(3)
                    # 给页面跳转预留加载时间
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                else:
                    print("[系统] 没有发现未完成或正在学的章节，可能已全部学完！等待 10 秒后重新检测...")
                    time.sleep(10)
                    # 可以在这里增加检测“下一页”或直接 break 退出循环
                    
            except PlaywrightTimeoutError:
                print("[网络/加载] 页面加载超时，尝试刷新并继续...")
                page.reload()
                time.sleep(5)
            except Exception as e:
                print(f"[错误] 主循环发生异常: {e}")
                time.sleep(5)

if __name__ == "__main__":
    run_auto_course()
