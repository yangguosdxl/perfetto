# mmap 真实物理内存归因
mmap_phys_analyzer.md
当修改了代码后，要运行**无栈malloc+mmap** 验证。
需要触发测试数据或采集样本时，不要使用 `adb monkey` 触发随机事件；应提醒用户在手机上按目标场景手动操作。
