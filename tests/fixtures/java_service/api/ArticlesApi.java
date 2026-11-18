package io.demo.api;

import io.demo.application.ArticleService;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(path = "/articles")
public class ArticlesApi {
  private ArticleService articleService;

  public ArticlesApi(ArticleService articleService) {
    this.articleService = articleService;
  }

  @PostMapping
  public ResponseEntity createArticle(@RequestBody NewArticleParam param) {
    Article article = articleService.createArticle(param).orElseThrow(InvalidRequestException::new);
    return ResponseEntity.status(201).body(article);
  }

  @GetMapping(path = "feed")
  public ResponseEntity getFeed(@RequestParam(value = "limit", defaultValue = "20") int limit) {
    return ResponseEntity.ok(articleService.findFeed(limit));
  }
}
