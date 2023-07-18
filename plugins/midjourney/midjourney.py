# encoding:utf-8

import plugins
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from channel.wechat.wechat_channel import WechatChannel

from common.log import logger
from common.expired_dict import ExpiredDict
from config import conf

from plugins import *
import base64
import os
import json
import requests
import schedule
import time
import threading


@plugins.register(
    name="Midjourney",
    desire_priority=-1,
    hidden=True,
    desc="AI drawing plugin of midjourney",
    version="1.0",
    author="littercoder",
)
class Midjourney(Plugin):
    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.proxy_server = conf().get("proxy_server")
        self.proxy_server = conf().get("proxy_api_secret")
        self.channel = WechatChannel()
        self.task_id_dict = ExpiredDict(60 * 60)
        self.cmd_dict = ExpiredDict(60 * 60)
        schedule.every(10).seconds.do(self.query_task_result)
        t = threading.Thread(target=self.run_task_polling)
        t.start()
        logger.info("[Midjourney] inited")

    def run_task_polling(self):
        while True:
            schedule.run_pending()
            time.sleep(1)

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type not in [ContextType.TEXT, ContextType.IMAGE]:
            return
        content = e_context["context"].content
        msg: ChatMessage = e_context["context"]["msg"]
        if not e_context["context"]["isgroup"]:
            state = "u:" + msg.other_user_id + ":" + msg.other_user_nickname
        else:
            state = "r:" + msg.other_user_id + ":" + msg.actual_user_nickname
        result = None
        try:
            if content.startswith("/imagine "):
                result = self.handle_imagine(content[9:], state)
            elif content.startswith("/up "):
                arr = content[4:].split()
                task_id = arr[0]
                index = int(arr[1])
                # 获取任务
                task = self.get_task(task_id)
                if task is None:
                    e_context["reply"] = Reply(ReplyType.TEXT, '任务ID不存在')
                    e_context.action = EventAction.BREAK_PASS
                    return
                # 获取按钮
                button = task['buttons'][index - 1]
                if button is None:
                    e_context["reply"] = Reply(ReplyType.TEXT, '按钮序号不正确')
                    e_context.action = EventAction.BREAK_PASS
                    return
                result = self.post_json('/submit/action', {'customId': button['customId'], 'taskId': task_id, 'state': state})
            elif content.startswith("/img2img "):
                self.cmd_dict[msg.actual_user_id] = content
                e_context["reply"] = Reply(ReplyType.TEXT, '请给我发一张图片作为垫图')
                e_context.action = EventAction.BREAK_PASS
                return
            elif content == "/describe":
                self.cmd_dict[msg.actual_user_id] = content
                e_context["reply"] = Reply(ReplyType.TEXT, '请给我发一张图片用于图生文')
                e_context.action = EventAction.BREAK_PASS
                return
            elif content.startswith("/shorten "):
                result = self.handle_shorten(content[9:], state)
            elif e_context["context"].type == ContextType.IMAGE:
                cmd = self.cmd_dict.get(msg.actual_user_id)
                if not cmd:
                    return
                msg.prepare()
                self.cmd_dict.pop(msg.actual_user_id)
                if "/describe" == cmd:
                    result = self.handle_describe(content, state)
                elif cmd.startswith("/img2img "):
                    result = self.handle_img2img(content, cmd[9:], state)
                else:
                    return
            else:
                return
        except Exception as e:
            logger.exception("[Midjourney] handle failed: %s" % e)
            result = {'code': -9, 'description': '服务异常, 请稍后再试'}
        code = result.get("code")
        if code == 1:
            task_id = result.get("result")
            self.add_task(task_id)
            e_context["reply"] = Reply(ReplyType.TEXT, '✅ 您的任务已提交\n🚀 正在快速处理中，请稍后\n📨 任务ID: '+ task_id)
        elif code == 22:
            self.add_task(result.get("result"))
            e_context["reply"] = Reply(ReplyType.TEXT, '✅ 您的任务已提交\n⏰ ' + result.get("description"))
        else:
            e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ ' + result.get("description"))
        e_context.action = EventAction.BREAK_PASS

    def handle_imagine(self, prompt, state):
        return self.post_json('/submit/imagine', {'prompt': prompt, 'state': state})

    def handle_describe(self, img_data, state):
        base64_str = self.image_file_to_base64(img_data)
        return self.post_json('/submit/describe', {'base64': base64_str, 'state': state})
    
    def handle_shorten(self, prompt, state):
        return self.post_json('/submit/shorten', {'prompt': prompt, 'state': state})

    def handle_img2img(self, img_data, prompt, state):
        base64_str = self.image_file_to_base64(img_data)
        return self.post_json('/submit/imagine', {'prompt': prompt, 'base64': base64_str, 'state': state})

    def post_json(self, api_path, data):
        return requests.post(url=self.proxy_server + api_path, json=data,
                             headers={'mj-api-secret': self.proxy_api_secret}).json()
    def get_task(self, task_id):
        return requests.get(url=self.proxy_server + '/task/%s/fetch' % task_id,
                             headers={'mj-api-secret': self.proxy_api_secret}).json()

    def get_help_text(self, **kwargs):
        help_text = "这是一个能调用midjourney实现ai绘图的扩展能力。\n"
        help_text += "使用说明: \n"
        help_text += "/imagine 根据给出的提示词绘画;\n"
        help_text += "/img2img 根据提示词+垫图生成图;\n"
        help_text += "/up 任务ID 序号执行动作;\n"
        help_text += "/describe 图片转文字;\n"
        help_text += "/shorten 提示词分析;"
        return help_text

    def add_task(self, task_id):
        self.task_id_dict[task_id] = 'NOT_START'

    def query_task_result(self):
        task_ids = list(self.task_id_dict.keys())
        if len(task_ids) == 0:
            return
        tasks = self.post_json('/task/list-by-condition', {'ids': task_ids})
        for task in tasks:
            task_id = task['id']
            description = task['description']
            status = task['status']
            action = task['action']
            state_array = task['state'].split(':', 2)
            context = Context()
            context.__setitem__("receiver", state_array[1])
            if state_array[0] == 'r':
                reply_prefix = '@%s ' % state_array[2]
            else:
                reply_prefix = ''
            if status == 'SUCCESS':
                self.task_id_dict.pop(task_id)
                if action == 'DESCRIBE' or action == 'SHORTEN':
                    prompt = task['properties']['finalPrompt']
                    reply = Reply(ReplyType.TEXT, (reply_prefix + '✅ 任务已完成\n📨 任务ID: %s\n%s\n\n' + self.get_buttons(task) + '\n' + '💡 使用 /up 任务ID 序号执行动作\n🔖 /up %s 1') % (
                                     task_id, prompt, task_id))
                else:
                    url_reply = Reply(ReplyType.IMAGE_URL, task['imageUrl'])
                    self.channel.send(url_reply, context)
                    reply = Reply(ReplyType.TEXT,
                                  ('✅ 任务已完成\n📨 任务ID: %s\n✨ %s\n\n' + self.get_buttons(
                                      task) + '\n' + '💡 使用 /up 任务ID 序号执行动作\n🔖 /up %s 1') % (
                                        task_id, description, task_id))
                self.channel.send(reply, context)
            elif status == 'MODAL':
                res = self.post_json('/submit/modal', {'taskId': task_id})
                if res.get("code") != 1:
                    self.task_id_dict.pop(task_id)
                    reply = Reply(ReplyType.TEXT,
                              reply_prefix + '❌ 任务执行失败\n✨ %s\n📨 任务ID: %s\n📒 失败原因: %s' % (task_id, res.get("description")))
                    self.channel.send(reply, context)
            elif status == 'FAILURE':
                self.task_id_dict.pop(task_id)
                reply = Reply(ReplyType.TEXT,
                              reply_prefix + '❌ 任务执行失败\n✨ %s\n📨 任务ID: %s\n📒 失败原因: %s' % (description, task['failReason']))
                self.channel.send(reply, context)

    def image_file_to_base64(self, file_path):
        with open(file_path, "rb") as image_file:
            img_data = image_file.read()
        img_base64 = base64.b64encode(img_data).decode("utf-8")
        os.remove(file_path)
        return "data:image/png;base64," + img_base64

    def get_buttons(self, task):
        res = ''
        index = 1
        for button in task['buttons']:
            name = button['emoji'] + button['label']
            if name in ['🎉Imagine all', '❤️']:
                continue
            res += ' %d- %s\n' % (index, name)
            index += 1
        return res
