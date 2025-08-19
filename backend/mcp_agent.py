"""
MCP智能体封装 - 为Web后端使用
基于 test.py 中的 SimpleMCPAgent，优化为适合WebSocket流式推送的版本
"""

import os
import json
import asyncio
from typing import Dict, List, Any, AsyncGenerator, Optional
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv, find_dotenv
import re
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
import contextvars

# ─────────── 1. MCP配置管理 ───────────
class MCPConfig:
    """MCP配置管理"""

    def __init__(self, config_file: str = "mcp.json"):
        self.config_file = config_file
        self.default_config = {}

    def load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        if Path(self.config_file).exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ 配置文件加载失败，使用默认配置: {e}")

        # 创建默认配置文件
        self.save_config(self.default_config)
        return self.default_config

    def save_config(self, config: Dict[str, Any]):
        """保存配置文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"❌ 配置文件保存失败: {e}")


# ─────────── 3. Web版MCP智能体 ───────────
class WebMCPAgent:
    """Web版MCP智能体 - 支持流式推送"""

    def __init__(self):
        # 修复：使用backend目录下的配置文件
        config_path = Path(__file__).parent / "mcp.json"
        self.config = MCPConfig(str(config_path))
        self.llm = None
        self.llm_tools = None  # 绑定工具用于判定与工具阶段
        self.mcp_client = None
        self.tools = []
        # 新增：按服务器分组的工具存储
        self.tools_by_server = {}
        self.server_configs = {}
        self._used_tool_names = set()

        # 加载 .env 并设置API环境变量（覆盖已存在的环境变量）
        try:
            load_dotenv(find_dotenv(), override=True)
        except Exception:
            # 忽略 .env 加载错误，继续从系统环境读取
            pass

        # 从环境变量读取配置
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.model_name = os.getenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL_NAME", "deepseek-chat")).strip()

        # 数值配置，带默认
        try:
            self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        except Exception:
            self.temperature = 0.2
        try:
            self.timeout = int(os.getenv("OPENAI_TIMEOUT", "60"))
        except Exception:
            self.timeout = 60

        # 将关键配置同步到环境（供底层SDK使用），不覆盖外部已设值
        if self.api_key and not os.getenv("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = self.api_key
        if self.base_url and not os.getenv("OPENAI_BASE_URL"):
            os.environ["OPENAI_BASE_URL"] = self.base_url

        # 会话上下文（存放每个 session 的数据等）
        self.session_contexts: Dict[str, Dict[str, Any]] = {}

        # 当前会话ID上下文变量（用于工具在运行时识别会话）
        self._current_session_id_ctx: contextvars.ContextVar = contextvars.ContextVar("current_session_id", default=None)

    async def initialize(self):
        """初始化智能体"""
        try:
            # 初始化大模型
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("缺少 OPENAI_API_KEY，请在 .env 或系统环境中配置")

            # ChatOpenAI 支持从环境变量读取 base_url
            base_llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                timeout=self.timeout,
                max_retries=3,
            )
            # 主引用向后兼容
            self.llm = base_llm

            # 加载MCP配置并连接
            mcp_config = self.config.load_config()
            self.server_configs = mcp_config.get("servers", {})

            # 允许没有外部MCP服务器
            if not self.server_configs:
                print("⚠️ 未配置外部MCP服务器，仅提供基础对话功能")
                self.server_configs = {}

            print("🔗 正在连接MCP服务器...")
            
            # 先测试服务器连接
            import aiohttp
            import asyncio
            
            for server_name, server_config in self.server_configs.items():
                try:
                    url = server_config.get('url')
                    if not url:
                        print(f"⚠️ 服务器 {server_name} 缺少 url 配置，跳过连接测试")
                        continue
                    print(f"🧪 测试连接到 {server_name}: {url}")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            print(f"✅ {server_name} 连接测试成功 (状态: {response.status})")
                except Exception as test_e:
                    print(f"⚠️ {server_name} 连接测试失败: {test_e}")
            
            # 创建MCP客户端 - 强制清除缓存并禁用HTTP/2
            import httpx

            def http_client_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(
                    http2=False,  # 禁用HTTP/2
                    headers=headers,
                    timeout=timeout,
                    auth=auth
                )

            # 更新服务器配置以使用自定义的httpx客户端工厂
            for server_name in self.server_configs:
                # 避免污染原配置对象，复制后添加工厂
                server_cfg = dict(self.server_configs[server_name])
                server_cfg['httpx_client_factory'] = http_client_factory
                self.server_configs[server_name] = server_cfg

            self.mcp_client = MultiServerMCPClient(self.server_configs)

            # 改为串行获取工具，避免并发问题
            print("🔧 正在逐个获取服务器工具...")
            for server_name in self.server_configs.keys():
                try:
                    print(f"─── 正在从服务器 '{server_name}' 获取工具 ───")
                    server_tools = await self.mcp_client.get_tools(server_name=server_name)
                    # 对工具名做合法化与去重
                    sanitized_tools = []
                    for tool in server_tools:
                        try:
                            original_name = getattr(tool, 'name', '') or ''
                            sanitized = self._sanitize_and_uniq_tool_name(original_name)
                            if sanitized != original_name:
                                print(f"🧹 规范化工具名: '{original_name}' -> '{sanitized}'")
                                try:
                                    tool.name = sanitized  # 覆盖名称，供后续绑定与匹配
                                except Exception:
                                    pass
                            sanitized_tools.append(tool)
                        except Exception as _e:
                            print(f"⚠️ 工具名规范化失败，跳过: {getattr(tool,'name','<unknown>')} - {_e}")
                            sanitized_tools.append(tool)
                    self.tools.extend(sanitized_tools)
                    self.tools_by_server[server_name] = sanitized_tools
                    print(f"✅ 从 {server_name} 获取到 {len(server_tools)} 个工具")
                except Exception as e:
                    print(f"❌ 从服务器 '{server_name}' 获取工具失败: {e}")
                    self.tools_by_server[server_name] = []
            
            # 本地医疗数据工具已移除，只使用 mcp.json 配置的外部工具
            print("ℹ️ 本地医疗数据工具未启用，仅使用 mcp.json 配置的外部工具")

            # 验证工具来源，确保只有配置文件中的服务器
            print(f"🔍 配置的服务器: {list(self.server_configs.keys())}")
            print(f"🔍 实际获取到的工具数量: {len(self.tools)}")
            
            # 分组逻辑已在上面的循环中完成，无需额外调用

            print(f"✅ 成功连接，获取到 {len(self.tools)} 个工具")
            print(f"📊 服务器分组情况: {dict((name, len(tools)) for name, tools in self.tools_by_server.items())}")

            # 创建工具判定实例（绑定工具，仅用于是否需要工具与参数生成）
            self.llm_tools = base_llm.bind_tools(self.tools)

            print("🤖 Web MCP智能助手已启动！")
            return True

        except Exception as e:
            import traceback
            print(f"❌ 初始化失败: {e}")
            print(f"📋 详细错误信息:")
            traceback.print_exc()
            
            # 尝试清理可能的连接
            if hasattr(self, 'mcp_client') and self.mcp_client:
                try:
                    await self.mcp_client.close()
                except:
                    pass
            return False

    def _get_tools_system_prompt(self) -> str:
        """用于工具判定/执行阶段的系统提示词：通用助手风格"""
        now = datetime.now()
        current_date = now.strftime("%Y年%m月%d日")
        current_weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        return (
            f"今天是 {current_date}（{current_weekday}）。你是一个有用、无害、诚实的AI助手。\n"
            "- 你可以使用可用的工具来帮助用户解决问题。\n"
            "- 当用户的问题需要获取实时信息、执行特定操作或使用外部服务时，请使用合适的工具。\n"
            "- 对于一般性问题、知识性问题或不需要工具的问题，请直接回答。\n"
            "- 如果决定使用工具，请只输出 tool_calls，不要同时输出自然语言回答。\n"
            "- 如果决定不使用工具，请提供有帮助的中文回答。\n"
        )

    def _get_stream_system_prompt(self) -> str:
        """保持接口以兼容旧调用，但当前不再使用流式回答提示词。"""
        return ""

    def _sanitize_and_uniq_tool_name(self, name: str) -> str:
        """将工具名规范为 ^[a-zA-Z0-9_-]+$，并避免重名冲突。"""
        if not isinstance(name, str):
            name = str(name or "")
        # 仅保留字母数字下划线和连字符，其余替换为下划线
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        if not sanitized:
            sanitized = "tool"
        base = sanitized
        # 确保唯一
        index = 1
        while sanitized in self._used_tool_names:
            index += 1
            sanitized = f"{base}_{index}"
        self._used_tool_names.add(sanitized)
        return sanitized

    async def chat_stream(self, user_input: str, history: List[Dict[str, Any]] = None, session_id: Optional[str] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """流式探测 + 立即中断：
        - 先直接 astream 开流，短暂缓冲并检测 function_call/tool_call；
        - 若检测到工具调用：立即中断本次流式（不下发缓冲），执行工具（非流式），写回 messages 后进入下一轮；
        - 若未检测到工具：将本次流作为最终回答，开始流式推送到结束。
        """
        try:
            if session_id:
                try:
                    self._current_session_id_ctx.set(session_id)
                except Exception:
                    pass
            print(f"🤖 开始处理用户输入: {user_input[:50]}...")
            yield {"type": "status", "content": "开始生成..."}

            # 1) 构建共享消息历史（不包含系统提示，便于两套系统提示分别注入）
            shared_history: List[Dict[str, Any]] = []
            if history:
                for record in history:
                    shared_history.append({"role": "user", "content": record['user_input']})
                    if record.get('ai_response'):
                        shared_history.append({"role": "assistant", "content": record['ai_response']})
            shared_history.append({"role": "user", "content": user_input})

            max_rounds = 25
            round_index = 0
            while round_index < max_rounds:
                round_index += 1
                print(f"🧠 第 {round_index} 轮推理 (双实例：判定工具 + 纯流式回答)...")

                # 2) 使用带工具实例做"流式判定"：
                tools_messages = [{"role": "system", "content": self._get_tools_system_prompt()}] + shared_history
                tool_calls_check = None
                buffered_chunks: List[str] = []
                content_preview = ""
                response_started = False
                try:
                    async for event in self.llm_tools.astream_events(tools_messages, version="v1"):
                        ev = event.get("event")
                        if ev == "on_chat_model_stream":
                            data = event.get("data", {})
                            chunk = data.get("chunk")
                            if chunk is None:
                                continue
                            try:
                                content_piece = getattr(chunk, 'content', None)
                            except Exception:
                                content_piece = None
                            if content_piece:
                                # 立即向前端流式下发作为最终回复
                                if not response_started:
                                    yield {"type": "ai_response_start", "content": "AI正在回复..."}
                                    response_started = True
                                buffered_chunks.append(content_piece)
                                try:
                                    print(f"📤 [判定LLM流] {content_piece}")
                                except Exception:
                                    pass
                                yield {"type": "ai_response_chunk", "content": content_piece}
                        elif ev == "on_chat_model_end":
                            data = event.get("data", {})
                            output = data.get("output")
                            try:
                                tool_calls_check = getattr(output, 'tool_calls', None)
                            except Exception:
                                tool_calls_check = None
                            try:
                                content_preview = getattr(output, 'content', None) or ""
                            except Exception:
                                content_preview = ""
                except Exception as e:
                    print(f"⚠️ 工具判定(流式)失败：{e}")
                    tool_calls_check = None
                    content_preview = ""

                if tool_calls_check:
                    if response_started:
                        print("⚠️ 检测到 tool_calls 但已开始输出文本流，按无工具处理以避免冲突")
                    tool_calls_to_run = tool_calls_check
                    yield {"type": "tool_plan", "content": f"AI决定调用 {len(tool_calls_to_run)} 个工具", "tool_count": len(tool_calls_to_run)}
                    # 写回assistant带tool_calls
                    try:
                        shared_history.append({
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls_to_run
                        })
                    except Exception:
                        shared_history.append({"role": "assistant", "content": ""})

                    # 执行工具（非流式）
                    exit_to_stream = False
                    for i, tool_call in enumerate(tool_calls_to_run, 1):
                        if isinstance(tool_call, dict):
                            tool_id = tool_call.get('id') or f"call_{i}"
                            fn = tool_call.get('function') or {}
                            tool_name = fn.get('name') or tool_call.get('name') or ''
                            tool_args_raw = fn.get('arguments') or tool_call.get('args') or {}
                        else:
                            tool_id = getattr(tool_call, 'id', None) or f"call_{i}"
                            tool_name = getattr(tool_call, 'name', '') or ''
                            tool_args_raw = getattr(tool_call, 'args', {}) or {}

                        # 解析参数
                        if isinstance(tool_args_raw, str):
                            try:
                                parsed_args = json.loads(tool_args_raw) if tool_args_raw else {}
                            except Exception:
                                parsed_args = {"$raw": tool_args_raw}
                        elif isinstance(tool_args_raw, dict):
                            parsed_args = tool_args_raw
                        else:
                            parsed_args = {"$raw": str(tool_args_raw)}

                        yield {"type": "tool_start", "tool_id": tool_id, "tool_name": tool_name, "tool_args": parsed_args, "progress": f"{i}/{len(tool_calls_to_run)}"}

                        try:
                            target_tool = None
                            for tool in self.tools:
                                if tool.name == tool_name:
                                    target_tool = tool
                                    break
                            if target_tool is None:
                                error_msg = f"工具 '{tool_name}' 未找到"
                                print(f"❌ {error_msg}")
                                yield {"type": "tool_error", "tool_id": tool_id, "error": error_msg}
                                tool_result = f"错误: {error_msg}"
                            else:
                                tool_result = await target_tool.ainvoke(parsed_args)
                                yield {"type": "tool_end", "tool_id": tool_id, "tool_name": tool_name, "result": str(tool_result)}
                                # 不再支持退出工具模式
                        except Exception as e:
                            error_msg = f"工具执行出错: {e}"
                            print(f"❌ {error_msg}")
                            yield {"type": "tool_error", "tool_id": tool_id, "error": error_msg}
                            tool_result = f"错误: {error_msg}"

                        # 始终追加 tool 消息，满足 OpenAI 函数调用协议要求
                        # 对于退出工具模式，内容为简单状态，不影响后续回答质量
                        shared_history.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "content": str(tool_result)
                        })

                        if exit_to_stream:
                            break

                    if exit_to_stream:
                        # 不再支持提前强制切流式，按原逻辑继续下一轮
                        pass
                    else:
                        # 工具后继续下一轮
                        continue

                # 3) 无工具：若已开始流式，则只补一个结束；否则一次性输出
                final_text = "".join(buffered_chunks) if buffered_chunks else (content_preview or "")
                if response_started:
                    yield {"type": "ai_response_end", "content": final_text}
                else:
                    yield {"type": "ai_response_start", "content": "AI正在回复..."}
                    if final_text:
                        try:
                            print(f"📤 [最终回复流] {final_text}")
                        except Exception:
                            pass
                        yield {"type": "ai_response_chunk", "content": final_text}
                    yield {"type": "ai_response_end", "content": final_text}
                return

            # 轮次耗尽：直接返回提示信息
            print(f"⚠️ 达到最大推理轮数({max_rounds})，直接返回提示信息")
            final_text = "已达到最大推理轮数，请缩小问题范围或稍后重试。"
            yield {"type": "ai_response_start", "content": "AI正在回复..."}
            yield {"type": "ai_response_chunk", "content": final_text}
            yield {"type": "ai_response_end", "content": final_text}
            return
        except Exception as e:
            import traceback
            print(f"❌ chat_stream 异常: {e}")
            print("📋 详细错误信息:")
            traceback.print_exc()
            yield {"type": "error", "content": f"处理请求时出错: {str(e)}"}

    def get_tools_info(self) -> Dict[str, Any]:
        """获取工具信息列表，按MCP服务器分组"""
        if not self.tools_by_server:
            return {"servers": {}, "total_tools": 0, "server_count": 0}
        
        servers_info = {}
        total_tools = 0
        
        # 按服务器分组构建工具信息
        for server_name, server_tools in self.tools_by_server.items():
            tools_info = []
            
            for tool in server_tools:
                tool_info = {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {},
                    "required": []
                }
                
                # 获取参数信息 - 优化版本
                try:
                    schema = None
                    
                    # 方法1: 尝试使用args_schema (LangChain工具常用)
                    if hasattr(tool, 'args_schema') and tool.args_schema:
                        if isinstance(tool.args_schema, dict):
                            schema = tool.args_schema
                        elif hasattr(tool.args_schema, 'model_json_schema'):
                            schema = tool.args_schema.model_json_schema()
                    
                    # 方法2: 如果没有args_schema，尝试tool_call_schema
                    if not schema and hasattr(tool, 'tool_call_schema') and tool.tool_call_schema:
                        schema = tool.tool_call_schema
                    
                    # 方法3: 最后尝试input_schema
                    if not schema and hasattr(tool, 'input_schema') and tool.input_schema:
                        if isinstance(tool.input_schema, dict):
                            schema = tool.input_schema
                        elif hasattr(tool.input_schema, 'model_json_schema'):
                            try:
                                schema = tool.input_schema.model_json_schema()
                            except:
                                pass
                    
                    # 解析schema
                    if schema and isinstance(schema, dict):
                        if 'properties' in schema:
                            tool_info["parameters"] = schema['properties']
                            tool_info["required"] = schema.get('required', [])
                        elif 'type' in schema and schema.get('type') == 'object' and 'properties' in schema:
                            tool_info["parameters"] = schema['properties']
                            tool_info["required"] = schema.get('required', [])
                
                except Exception as e:
                    # 如果出错，至少保留工具的基本信息
                    print(f"⚠️ 获取工具 '{tool.name}' 参数信息失败: {e}")
                
                tools_info.append(tool_info)
            
            # 添加服务器信息
            servers_info[server_name] = {
                "name": server_name,
                "tools": tools_info,
                "tool_count": len(tools_info)
            }
            
            total_tools += len(tools_info)
        
        return {
            "servers": servers_info,
            "total_tools": total_tools,
            "server_count": len(servers_info)
        }

    async def close(self):
        """关闭连接"""
        try:
            if self.mcp_client and hasattr(self.mcp_client, 'close'):
                await self.mcp_client.close()
        except:
            pass
