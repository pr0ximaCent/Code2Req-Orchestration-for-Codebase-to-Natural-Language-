package io.demo.application;

import io.demo.core.ArticleRepository;
import io.demo.infrastructure.ArticleReadService;

@Service
public class ArticleService {
  private ArticleRepository articleRepository;
  private ArticleReadService articleReadService;

  public Optional<Article> createArticle(NewArticleParam param) {
    Article article = new Article(param.getTitle());
    articleRepository.save(article);
    return Optional.of(article);
  }

  public List<Article> findFeed(int limit) {
    return articleReadService.findFeed(limit);
  }
}
