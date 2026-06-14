import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from cjquant.optimizer.traditional import RiskParityOptimizer

def init(context):
    print(f"Initializing ML-based Meta-Learning Strategy with funds: {context.funds}")
    context.last_month = None
    
    context.train_history = 252  # 获取约1年的日线数据进行滚动训练
    context.window_size = 20     # 特征/目标计算窗口（约1个月）

def extract_features(df_returns_chunk):
    mean_ret = df_returns_chunk.mean().mean()  # 整体平均收益
    mean_vol = df_returns_chunk.std().mean()   # 整体平均波动率
    
    # 计算资产间平均相关系数
    corr_matrix = df_returns_chunk.corr().values
    upper_tri = corr_matrix[np.triu_indices(corr_matrix.shape[0], k=1)]
    mean_corr = upper_tri.mean() if len(upper_tri) > 0 else 0.0
    
    return [mean_ret, mean_vol, mean_corr]

def calculate_base_weights(df_chunk, funds):
    try:
        opt = RiskParityOptimizer(df_chunk)
        w_rp = opt.optimize().to_dict()
    except:
        w_rp = {f: 1.0 / len(funds) for f in funds}
        
    cum_rets = (1 + df_chunk).prod() - 1
    exp_rets = np.exp(cum_rets - np.max(cum_rets))
    w_mom = (exp_rets / np.sum(exp_rets)).to_dict()
    
    return w_rp, w_mom

def handle_bar(context):
    current_date = context.current_date
    is_first_day = (context.current_date_idx == 0)
    is_new_month = (context.last_month is not None and current_date.month != context.last_month)
    context.last_month = current_date.month
    
    if is_first_day:
        print(f"[{current_date.strftime('%Y-%m-%d')}] Day 1: Equal weight initial allocation.")
        weights = {fund: 1.0 / len(context.funds) for fund in context.funds}
        context.rebalance(weights)
        return

    if is_new_month:
        print(f"[{current_date.strftime('%Y-%m-%d')}] Monthly trigger: Training ML Meta-Regressor and rebalancing.")
        
        try:
            hist_returns = {}
            for fund in context.funds:
                navs = context.get_history_navs(fund, count=context.train_history + 10)
                if len(navs) > context.train_history:
                    hist_returns[fund] = navs.pct_change().dropna()
            
            df_returns = pd.DataFrame(hist_returns).dropna()
            
            if len(df_returns) < context.train_history:
                raise ValueError("Insufficient history for ML training.")

            X_train = []
            y_train = []
            
            step = context.window_size
            total_len = len(df_returns)
            
            # 留出最后一组作为当前特征进行预测，前面的用于训练
            for i in range(0, total_len - 2 * step, step):
                # 支持区间：提取特征并计算当时的策略权重
                df_support = df_returns.iloc[i : i + step]
                # 查询区间：计算策略在未来一个月的实际表现
                df_query = df_returns.iloc[i + step : i + 2 * step]
                
                # 提取特征
                features = extract_features(df_support)
                
                # 计算支持区间的策略权重
                w_rp, w_mom = calculate_base_weights(df_support, context.funds)
                
                # 计算查询区间各自策略的实际收益
                w_rp_vec = np.array([w_rp.get(f, 0.0) for f in df_query.columns])
                w_mom_vec = np.array([w_mom.get(f, 0.0) for f in df_query.columns])
                
                ret_rp = df_query.dot(w_rp_vec).mean()
                ret_mom = df_query.dot(w_mom_vec).mean()
                
                # 训练目标：动量策略相比于风险平价策略的超额收益
                target = ret_mom - ret_rp
                
                X_train.append(features)
                y_train.append(target)
            
            X_train = np.array(X_train)
            y_train = np.array(y_train)
            
            # 使用浅层随机森林，避免小样本过拟合
            model = RandomForestRegressor(n_estimators=50, max_depth=3, random_state=42)
            model.fit(X_train, y_train)
            
            df_current = df_returns.iloc[-context.window_size:]
            current_features = np.array([extract_features(df_current)])
            
            predicted_diff = model.predict(current_features)[0]
            
            # 将预测的超额收益通过 Sigmoid 映射为动量策略的权重 alpha (0 到 1 之间)
            # 缩放因子 50 用于平滑变化
            alpha = 1.0 / (1.0 + np.exp(-50.0 * predicted_diff))
            
            # 计算当前调仓日两者的基础权重
            w_rp_curr, w_mom_curr = calculate_base_weights(df_current, context.funds)
            
            # 融合权重
            weights = {}
            for fund in context.funds:
                weights[fund] = alpha * w_mom_curr.get(fund, 0.0) + (1.0 - alpha) * w_rp_curr.get(fund, 0.0)
            
            # 归一化
            total_w = sum(weights.values())
            if total_w > 0:
                weights = {k: v / total_w for k, v in weights.items()}
            else:
                weights = {fund: 1.0 / len(context.funds) for fund in context.funds}
                
            print(f"Predicted Performance Diff (Mom - RP): {predicted_diff:.6f}, Meta Weight (Alpha for Momentum): {alpha:.2%}")
            
        except Exception as e:
            print(f"ML Meta-Learning pipeline failed: {e}. Falling back to equal weights.")
            weights = {fund: 1.0 / len(context.funds) for fund in context.funds}
            
        print(f"New target allocation: {weights}")
        context.rebalance(weights)