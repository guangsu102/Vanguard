-- =============================================================================
-- Vanguard Database Migration
-- Version: 007
-- Description: Seed initial sensitive keywords for moderation
-- =============================================================================

-- Up Migration

-- Insert initial competitor keywords (竞品关键词)
INSERT INTO keyword (text, type, status, match_mode, trigger_count, created_at, updated_at) VALUES
-- VPN/机场 相关
('机场', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('节点', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('梯子', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('VPN', 'competitor', 'approved', 'exact', 0, NOW(), NOW()),
('加速器', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('翻墙', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('科学上网', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('代理', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),

-- 技术协议
('v2ray', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('clash', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('ssr', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('trojan', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('wireguard', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),

-- 常见竞品词
('免费节点', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('低价机场', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('月付', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('年付', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),
('流量套餐', 'competitor', 'approved', 'fuzzy', 0, NOW(), NOW()),

-- 竞品变体词（待审核）
('机+场', 'competitor', 'pending', 'fuzzy', 0, NOW(), NOW()),
('机勾', 'competitor', 'pending', 'fuzzy', 0, NOW(), NOW()),
('鸡场', 'competitor', 'pending', 'fuzzy', 0, NOW(), NOW()),
('小鸡', 'competitor', 'pending', 'fuzzy', 0, NOW(), NOW()),
('纸飞机', 'competitor', 'pending', 'fuzzy', 0, NOW(), NOW()),

-- 引流关键词 - 需求类
('VPN推荐', 'demand', 'approved', 'fuzzy', 0, NOW(), NOW()),
('翻墙工具', 'demand', 'approved', 'fuzzy', 0, NOW(), NOW()),
('机场服务', 'demand', 'approved', 'fuzzy', 0, NOW(), NOW()),
('稳定节点', 'demand', 'approved', 'fuzzy', 0, NOW(), NOW()),
('高速梯子', 'demand', 'approved', 'fuzzy', 0, NOW(), NOW()),

-- 咨询类关键词
('怎么翻墙', 'inquiry', 'approved', 'fuzzy', 0, NOW(), NOW()),
('节点怎么用', 'inquiry', 'approved', 'fuzzy', 0, NOW(), NOW()),
('支持哪些设备', 'inquiry', 'approved', 'fuzzy', 0, NOW(), NOW()),
('速度如何', 'inquiry', 'approved', 'fuzzy', 0, NOW(), NOW()),
('有试用吗', 'inquiry', 'approved', 'fuzzy', 0, NOW(), NOW()),

-- 价格类关键词
('VPN价格', 'price', 'approved', 'fuzzy', 0, NOW(), NOW()),
('机场多少钱', 'price', 'approved', 'fuzzy', 0, NOW(), NOW()),
('月费多少', 'price', 'approved', 'fuzzy', 0, NOW(), NOW()),
('有优惠吗', 'price', 'approved', 'fuzzy', 0, NOW(), NOW()),
('新人折扣', 'price', 'approved', 'fuzzy', 0, NOW(), NOW());

-- Down Migration
-- DELETE FROM keyword WHERE trigger_count = 0 AND created_at >= DATE_SUB(NOW(), INTERVAL 1 DAY);
