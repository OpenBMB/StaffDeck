"""飞书官方 lark-cli 集成（受信包装层）。

模型只能通过 ``lark_cli`` 内置能力传结构化 argv；本包负责：
二进制供给（provision）、子命令策略（policy）、每用户 HOME 与凭据
注入及进程执行（runner）、以及带确认闸的调用入口（service）。
"""
