# 共享构建缓存清理方案（待授权，尚未执行）

目标：oracle4c24g / 168.110.23.229。Vanguard 位于 /opt/vanguard；Docker 构建缓存为多个项目共用。

当前快照磁盘约 84%；BuildKit 私有可回收缓存约 8.584 GB。按最后使用时间粗略统计，至少 12 小时未使用的私有记录约 8.488 GB，至少 24 小时仅约 0.117 GB。记录存在依赖与共享关系，以上不是承诺可释放空间。

建议精确命令：

```sh
docker buildx prune --filter until=12h --keep-storage 6GB --force
```

仅清理 BuildKit 判定未使用且满足时间条件的构建缓存，并以 6 GB 为保留目标；受 Docker 版本与缓存依赖影响，不保证最终大小。不会运行 image/system/volume prune，也不删除运行容器、镜像标签、数据库、备份、源码或日志。暂不删除那 23 个缺少文件引用的 Vanguard 镜像标签，其中有 rollback 命名且可能承担未记录的恢复用途。

代价：Sub2API 等其他项目下一次构建可能需要重新下载或执行部分构建步骤。这些缓存没有逐项目所有权标记，因此本次 Vanguard 部署授权尚不能明确涵盖共享缓存清理。

执行前后验证：记录 df 的 available 与使用率、docker system df、全部现有容器的 ID/镜像/运行状态及启动时间；确认运行容器和镜像引用均未变、Vanguard 公网 health 为 200、受保护 API 为 401，并输出实际回收字节数。若用户不授权，则只保留清单，不执行清理。
