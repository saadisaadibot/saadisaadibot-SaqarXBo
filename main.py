from pytrends.request import TrendReq

pytrends = TrendReq()
trending = pytrends.trending_searches(pn="global")
print(trending.head())