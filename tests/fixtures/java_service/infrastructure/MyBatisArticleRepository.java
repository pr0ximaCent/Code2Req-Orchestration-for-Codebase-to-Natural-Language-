package io.demo.infrastructure;

import io.demo.core.ArticleRepository;

@Repository
public class MyBatisArticleRepository implements ArticleRepository {
  private ArticleMapper articleMapper;

  @Override
  public void save(Article article) {
    articleMapper.insert(article);
  }
}
